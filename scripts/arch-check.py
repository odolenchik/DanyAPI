#!/usr/bin/env python3
"""DanyAPI architecture invariant checker.

Enforces the hard architectural constraints from ``AGENTS.md``:

* no circular imports between ``danyapi.api``, ``danyapi.tools``,
  ``danyapi.deepseek`` and ``danyapi.qwen``;
* no ``eval()`` / ``exec()`` / ``compile()`` calls;
* no ``pickle.loads()`` / ``marshal.loads()`` on untrusted input;
* no raw SQL (``sqlite3`` / ``psycopg`` / ``asyncpg`` / SQLAlchemy imports,
  or ``.execute()`` / ``.executemany()`` calls).

Exit code: 0 when every invariant holds, 1 when violations are found,
2 on an internal error. Uses only the standard library, so it can be run
directly with ``./scripts/arch-check.py`` without the project venv.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "danyapi"

PACKAGES = {"api", "tools", "deepseek", "qwen", "zai"}

BANNED_CALL_NAMES = {"eval", "exec", "compile"}
BANNED_LOAD_ATTRIBUTES = {("pickle", "loads"), ("marshal", "loads")}
BANNED_SQL_MODULES = {"sqlite3", "psycopg", "psycopg2", "asyncpg", "sqlalchemy"}
BANNED_SQL_CALL_ATTRIBUTES = {"execute", "executemany"}


def iter_python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py"))


def parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def module_parts(path: Path) -> list[str]:
    return list(path.relative_to(ROOT).with_suffix("").parts)


def package_parts(path: Path) -> list[str]:
    parts = module_parts(path)
    if path.name == "__init__.py":
        return parts
    return parts[:-1]


def component_of(path: Path) -> str | None:
    rel = path.relative_to(SRC)
    parts = rel.parts
    if len(parts) == 1:
        return path.stem
    return parts[0]


def component_from_dotted(dotted: str) -> str | None:
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[0] == "danyapi" and parts[1] in PACKAGES:
        return parts[1]
    return None


def resolve_import_from(pkg_parts: list[str], node: ast.ImportFrom, alias: ast.alias) -> str | None:
    if node.level == 0:
        return node.module
    base = pkg_parts[: len(pkg_parts) - node.level + 1]
    if node.module:
        base += node.module.split(".")
    if alias.name != "*":
        base += [alias.name.split(".")[0]]
    return ".".join(base)


def collect_import_edges(files: list[Path]) -> dict[str, dict[str, list[tuple[Path, int]]]]:
    edges: dict[str, dict[str, list[tuple[Path, int]]]] = defaultdict(lambda: defaultdict(list))
    for file in files:
        src_comp = component_of(file)
        if src_comp not in PACKAGES:
            continue
        tree = parse(file)
        pkg_parts = package_parts(file)
        rel = file.relative_to(ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    dst = component_from_dotted(alias.name)
                    if dst and dst != src_comp:
                        edges[src_comp][dst].append((rel, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    dotted = resolve_import_from(pkg_parts, node, alias)
                    dst = component_from_dotted(dotted) if dotted else None
                    if dst and dst != src_comp:
                        edges[src_comp][dst].append((rel, node.lineno))
    return edges


def canonical_cycle(cycle: list[str]) -> tuple[str, ...]:
    best: tuple[str, ...] | None = None
    for seq in (cycle, list(reversed(cycle))):
        for i in range(len(seq)):
            rotated = tuple(seq[i:] + seq[:i])
            if best is None or rotated < best:
                best = rotated
    return best or ()


def find_cycles(edges: dict[str, dict[str, list[tuple[Path, int]]]]) -> list[list[str]]:
    graph: dict[str, list[str]] = {}
    for node in PACKAGES:
        graph[node] = sorted(edges.get(node, {}))
    color: dict[str, int] = {}
    stack: list[str] = []
    seen: set[tuple[str, ...]] = set()
    cycles: list[list[str]] = []

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt, 0) == 0:
                dfs(nxt)
            elif color.get(nxt) == 1:
                start = stack.index(nxt)
                cycle = stack[start:]
                canon = canonical_cycle(cycle)
                if canon not in seen:
                    seen.add(canon)
                    cycles.append(cycle)
        stack.pop()
        color[node] = 2

    for node in sorted(graph):
        if color.get(node, 0) == 0:
            dfs(node)
    return cycles


def check_cycles(files: list[Path]) -> list[str]:
    edges = collect_import_edges(files)
    cycles = find_cycles(edges)
    violations: list[str] = []
    for cycle in cycles:
        loop = [*cycle, cycle[0]]
        violations.append("circular import: " + " -> ".join(loop))
        for src, dst in zip(cycle, loop[1:], strict=True):
            for rel, line in edges.get(src, {}).get(dst, []):
                violations.append(f"{rel}:{line}: {src} imports {dst}")
    return violations


def check_dangerous_calls(files: list[Path]) -> list[str]:
    violations: list[str] = []
    for file in files:
        tree = parse(file)
        rel = file.relative_to(ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALL_NAMES:
                violations.append(f"{rel}:{node.lineno}: forbidden call {func.id}()")
            elif isinstance(func, ast.Attribute) and func.attr == "loads" and isinstance(func.value, ast.Name) and func.value.id in {"pickle", "marshal"}:
                violations.append(f"{rel}:{node.lineno}: forbidden deserialization {func.value.id}.loads()")
    return violations


def check_raw_sql(files: list[Path]) -> list[str]:
    violations: list[str] = []
    for file in files:
        tree = parse(file)
        rel = file.relative_to(ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in BANNED_SQL_MODULES:
                        violations.append(f"{rel}:{node.lineno}: raw SQL: forbidden import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    if top in BANNED_SQL_MODULES:
                        violations.append(f"{rel}:{node.lineno}: raw SQL: forbidden import from {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in BANNED_SQL_CALL_ATTRIBUTES:
                    violations.append(f"{rel}:{node.lineno}: raw SQL: forbidden call .{func.attr}()")
    return violations


def check_docstrings(files: list[Path]) -> list[str]:
    violations: list[str] = []
    for file in files:
        tree = parse(file)
        rel = file.relative_to(ROOT)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_") and not ast.get_docstring(node):
                    violations.append(f"{rel}:{node.lineno}: public {type(node).__name__.lower()} '{node.name}' has no docstring")
    return violations


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check DanyAPI architectural invariants.")
    parser.add_argument("--docstrings", action="store_true", help="also require docstrings on public functions/classes")
    args = parser.parse_args(argv[1:])

    files = iter_python_files()
    checks = [check_cycles, check_dangerous_calls, check_raw_sql]
    if args.docstrings:
        checks.append(check_docstrings)

    violations: list[str] = []
    for check in checks:
        try:
            violations.extend(check(files))
        except Exception as exc:
            print(f"arch-check: internal error in {check.__name__}: {exc}", file=sys.stderr)
            return 2

    if violations:
        for line in violations:
            print(line, file=sys.stderr)
        print(f"arch-check: {len(violations)} violation(s) found", file=sys.stderr)
        return 1

    print("arch-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
