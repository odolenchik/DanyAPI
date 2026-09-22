#!/usr/bin/env python3
"""DanyAPI preflight checks, run via ``make preflight``.

Each check maps to a subcommand so individual failures are easy to reproduce:

    python scripts/preflight.py env-check
    python scripts/preflight.py deps
    python scripts/preflight.py db-ping
    python scripts/preflight.py app-check
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RUNTIME_IMPORTS = ("fastapi", "uvicorn", "httpx", "pydantic", "dotenv")


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read a .env file into a dict. Missing/unreadable file yields {}."""
    env: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def _credentials(env: dict[str, str]):
    def parse_tokens(key: str) -> list[str]:
        return [t.strip() for t in env.get(key, "").split(",") if t.strip()]

    def provider(multi_key: str, single_key: str, email_key: str, password_key: str) -> tuple[bool, str]:
        tokens = parse_tokens(multi_key)
        single = env.get(single_key, "").strip()
        if not tokens and single:
            tokens = [single]
        email = env.get(email_key, "").strip()
        password = env.get(password_key, "").strip()
        if tokens:
            return True, f"{len(tokens)} token(s)"
        if email:
            if not password:
                print(f"PREFLIGHT FAILED: {email_key} is set but {password_key} is empty", file=sys.stderr)
                raise SystemExit(1)
            return True, "email+password"
        return False, ""

    deepseek = provider("DEEPSEEK_TOKENS", "DEEPSEEK_TOKEN", "DEEPSEEK_EMAIL", "DEEPSEEK_PASSWORD")
    qwen = provider("QWEN_TOKENS", "QWEN_TOKEN", "QWEN_EMAIL", "QWEN_PASSWORD")
    return deepseek, qwen


def env_check() -> None:
    env = _read_dotenv(ROOT / ".env")
    # Real environment wins over .env, matching python-dotenv's override=False.
    env = {**env, **os.environ}
    (deepseek_ok, ds_desc), (qwen_ok, qw_desc) = _credentials(env)
    if not deepseek_ok and not qwen_ok:
        print(
            "PREFLIGHT FAILED: no provider credentials configured. In .env set at least one of "
            "DEEPSEEK_TOKENS, DEEPSEEK_TOKEN, DEEPSEEK_EMAIL or QWEN_TOKENS, QWEN_TOKEN, QWEN_EMAIL.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"  [OK] provider credentials present (deepseek: {ds_desc or 'none'}, qwen: {qw_desc or 'none'})")


def deps_check() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(ROOT / "requirements.txt")],
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        print("PREFLIGHT FAILED: pip install -r requirements.txt failed", file=sys.stderr)
        raise SystemExit(1)

    missing = []
    for name in RUNTIME_IMPORTS:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        print(f"PREFLIGHT FAILED: missing importable packages: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(1)
    print("  [OK] dependencies installed and importable")


def db_ping() -> None:
    from danyapi.config import settings
    from danyapi.store import JsonStore

    with tempfile.TemporaryDirectory() as tmp:
        settings.cache_dir = tmp
        key = "__preflight_ping__"
        store = JsonStore("preflight", "ping")
        store.set(key, "pong")
        reloaded = JsonStore("preflight", "ping")
        if reloaded.get(key) != "pong":
            print(
                "PREFLIGHT FAILED: JsonStore write did not survive reload (check cache directory writability)",
                file=sys.stderr,
            )
            raise SystemExit(1)
        store.pop(key)
    print("  [OK] db-ping: JsonStore persistence works")


def app_check() -> None:
    try:
        import danyapi.api.openai  # noqa: F401
    except Exception as exc:
        print(f"PREFLIGHT FAILED: application import failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("  [OK] app-check: FastAPI app imports cleanly (no DB migrations in this project)")


COMMANDS = {
    "env-check": env_check,
    "deps": deps_check,
    "db-ping": db_ping,
    "app-check": app_check,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print(f"usage: {argv[0]} <{'|'.join(COMMANDS)}>", file=sys.stderr)
        return 2
    COMMANDS[argv[1]]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
