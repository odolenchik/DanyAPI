"""Merge tool-trace NDJSON events into per-request records and print a summary.

Joins the four correlated events (client / dany / upstream / parse) by
``request_id``, then prints:

* a short report of mangle patterns (top ``parse.issue`` values),
* an optional dump of every merged request as NDJSON on stdout,
* and writes a merged JSON array to ``--out`` when requested.

Usage::

    python scripts/merge_traces.py tooltrace.ndjson [more.ndjson ...] \
        [--out merged.json] [--dump-ndjson] [--limit 20]

Multiple input files are concatenated (rotated backups included). Events with no
matching request_id group are kept under a synthetic ``_orphan`` bucket so nothing
is silently dropped.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _load_events(paths: list[Path]) -> list[dict]:
    events: list[dict] = []
    for p in paths:
        if not p.exists():
            print(f"warn: missing file {p}", file=sys.stderr)
            continue
        with p.open(encoding="utf-8") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                raw = raw_line.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError as exc:
                    print(f"warn: {p}:{line_no}: bad NDJSON ({exc})", file=sys.stderr)
                    continue
                events.append(rec)
    return events


def _merge(events: list[dict]) -> dict[str, dict]:
    by_rid: dict[str, dict] = defaultdict(dict)
    for e in events:
        rid = e.get("request_id") or "_orphan"
        actor = e.get("actor", "?")
        # If the same (rid, actor) pair appears twice, keep the latest ts.
        slot = by_rid[rid].setdefault(actor, {})
        if "ts" not in slot or e.get("ts", 0) >= slot.get("ts", -1):
            slot.update(e)
    # Normalise: each request gets a flat view + an ordered event list.
    merged: dict[str, dict] = {}
    for rid, actors in by_rid.items():
        rec: dict = {"request_id": rid}
        for actor in ("client", "dany", "upstream", "parse"):
            if actor in actors:
                # Copy the relevant fields without nesting.
                src = actors[actor]
                for k, v in src.items():
                    key = f"{actor}.{k}" if k not in ("request_id", "ts") else k
                    rec.setdefault(key, v)
        rec["actors_present"] = sorted(actors.keys())
        merged[rid] = rec
    return merged


def _summary(merged: dict[str, dict]) -> None:
    total = len(merged)
    full = sum(1 for r in merged.values() if set(r.get("actors_present", [])) == {"client", "dany", "upstream", "parse"})
    only_client = sum(1 for r in merged.values() if set(r.get("actors_present", [])) <= {"client"})

    # Parse-result distribution.
    parse_results: Counter[str] = Counter()
    issue_counter: Counter[str] = Counter()
    model_counter: Counter[str] = Counter()
    degen_models: Counter[str] = Counter()
    tool_names_seen: set[str] = set()
    for r in merged.values():
        res = r.get("parse.result")
        if res:
            parse_results[res.split(":")[0]] += 1  # ok / unparsed / no_tool_call ...
        if res == "degenerate_repeat":
            degen_models[r.get("client.model") or r.get("dany.model") or "?"] += 1
        issue = r.get("parse.issue") or ""
        if issue and not str(issue).startswith(("no", "none")):
            # Aggregate by the leading token of each comma-separated issue entry.
            for part in [p.strip() for p in issue.split(",")]:
                if part:
                    head = part.split(".")[0]  # tool name or error kind
                    issue_counter[head] += 1
        model = r.get("client.model") or r.get("dany.model")
        if model:
            model_counter[model] += 1
        for t in r.get("client.tool_names", []) or []:
            tool_names_seen.add(t)

    print("=" * 60)
    print(f"total request_ids : {total}")
    print(f"full (4 events)   : {full}")
    print(f"client-only       : {only_client}  (no tools / no upstream yet)")
    print("-" * 60)
    if parse_results:
        print("parse result distribution:")
        for k, n in parse_results.most_common():
            print(f"  {k:<14}: {n}")
    else:
        print("no parse events (all requests were non-tool or pre-upstream)")
    if issue_counter:
        print("\ntop mangle patterns (by leading token of parse.issue):")
        for k, n in issue_counter.most_common(15):
            print(f"  {k:<28}: {n}")
    else:
        print("\nno issues recorded")
    if degen_models:
        total_degen = sum(degen_models.values())
        print(f"\ndegenerate repeats (aborted runaway calls-container loops): {total_degen}")
        for k, n in degen_models.most_common():
            print(f"  {k:<32}: {n}")
        print("  hint: long multi-call turns can lose EOS and repeat the empty DSML container; Dany aborts the stream (DANYAPI_DEGENERATE_GUARD).")
    if model_counter:
        print("\nmodels:")
        for k, n in model_counter.most_common():
            print(f"  {k:<32}: {n}")
    if tool_names_seen:
        print(f"\ndistinct tools seen: {len(tool_names_seen)}")


def _dump_ndjson(merged: dict[str, dict], limit: int | None) -> None:
    for i, rec in enumerate(sorted(merged.values())):
        if limit is not None and i >= limit:
            break
        print(json.dumps(rec, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path, help="NDJSON file(s), including rotated backups")
    ap.add_argument("--out", type=Path, default=None, help="write merged JSON array to this path")
    ap.add_argument("--dump-ndjson", action="store_true", help="print every merged request as NDJSON on stdout")
    ap.add_argument("--limit", type=int, default=None, help="cap --dump-ndjson output")
    args = ap.parse_args(argv)

    events = _load_events(args.inputs)
    if not events:
        print("no events loaded", file=sys.stderr)
        return 1

    merged = _merge(events)
    _summary(merged)

    if args.dump_ndjson:
        print("-" * 60, file=sys.stdout)
        print("# merged per-request records (NDJSON)", file=sys.stdout)
        _dump_ndjson(merged, args.limit)

    if args.out is not None:
        payload = [merged[rid] for rid in sorted(merged)]
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {len(payload)} records to {args.out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
