"""Standalone smoke test for the tool-trace pipeline.

Implements one full request path (client -> dany -> upstream -> parse) with a
deliberately mangled DeepSeek response (extra ``offset`` argument), then reads
the NDJSON file back and asserts all four correlated events landed under a
single request_id. Run: ``python scripts/smoke_tooltrace.py [out.ndjson]``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import danyapi.api.openai as openai_mod  # noqa: E402
from danyapi.tooltrace import ToolTraceLogger  # noqa: E402

# Base SSE is the same working fixture used by tests/test_tools_api.py; we splice in an
# extra ``offset`` argument to simulate a DeepSeek mangle (extra key not in the schema).
_DS_BASE = (
    "event: ready\n"
    'data: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n'
    "\n"
    'data: {"v":{"response":{"message_id":2,"parent_id":1,"status":"WIP","fragments":['
    '{"id":2,"type":"RESPONSE","content":"{\\"tool_calls\\": [{\\"name\\": \\"get_weather\\""}]}}}\n'
    "\n"
    'data: {"p":"response/fragments/-1/content","o":"APPEND","v":", \\"arguments\\": '
    '{\\"city\\": \\"Moscow\\"}}]}"}\n'
    "\n"
    'data: {"p":"response/status","o":"SET","v":"FINISHED"}\n'
    "\n"
)

# Splice in an extra ``offset`` key right after the city argument -> mangled tool_call.
DS_MANGLED_SSE = _DS_BASE.replace(
    '{\\"city\\": \\"Moscow\\"}}]}',
    '{\\"city\\": \\"Moscow\\", \\"offset\\": 5}}]}',
)


class FakeSession:
    def __init__(self, sid="smoke-c1"):
        self.id = sid
        self.last_message_id = None
        self.last_response_id = None


class FakeResp:
    def __init__(self, sse_text: str):
        self._b = sse_text.encode()
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream; charset=utf-8"}

    async def aiter_bytes(self):
        yield self._b

    async def aclose(self):
        pass

    async def aread(self):
        return self._b


class FakeAccount:
    def __init__(self, sse_list):
        self.index = 0
        self.broken = False
        self.client = MagicMock()
        self.client.completion = AsyncMock(side_effect=[FakeResp(s) for s in sse_list])
        self.client.create_pow_challenge = AsyncMock(return_value={})
        self.pow = MagicMock()
        self.pow.make_header = AsyncMock(return_value={})
        self.sem = asyncio.Semaphore(1)
        self.sessions = MagicMock()
        self.sessions.obtain = AsyncMock(return_value=(FakeSession(), "smoke-sid"))
        self.sessions.touch_last_message = MagicMock()


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(rec) for rec in path.read_text(encoding="utf-8").splitlines() if rec.strip()]


async def main(out_path: Path) -> int:
    # Zero the backoff so a single fake completion is enough.
    orig = openai_mod.RETRY_BACKOFF_SEC
    openai_mod.RETRY_BACKOFF_SEC = 0.0

    request_id = "smoke-req-1"
    trace = ToolTraceLogger(str(out_path))
    tool_schemas = {"get_weather": {"city": "string"}}

    # Mark the start time so we can isolate THIS run's events in an append-only file.
    import time as _time

    started_at = _time.time()

    # --- client event (what the OpenAI-compatible caller declared) ---
    trace.client(
        request_id,
        session_id="smoke-sid",
        model="deepseek-v4-flash",
        tools=[{"type": "function", "function": {"name": "get_weather"}}],
        tool_choice=None,
        messages_count=1,
    )

    # --- dany + upstream + parse events via the real collect path ---
    acct = FakeAccount([DS_MANGLED_SSE])
    args = {
        "account": acct,
        "pool": MagicMock(),
        "existing_sid": "smoke-sid",
        "lock": acct.sem,
        "prompt": '{"tool_calls":[{"name":"get_weather","arguments":{"city":"Moscow"}}]}',
        "model": "deepseek-v4-flash",
        "model_type": "default",
        "thinking": False,
        "search": False,
        "tool_mode": True,
        "request_id": request_id,
        "trace": trace,
        "tool_schemas": tool_schemas,
    }
    result = await openai_mod._collect_non_stream(**args)

    # --- verify the response is still a valid OpenAI tool_call despite mangling ---
    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls", f"unexpected finish_reason {choice['finish_reason']}"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "get_weather"

    # --- read the NDJSON back and check all four events are correlated ---
    events = _read_events(out_path)
    ours = [e for e in events if e.get("request_id") == request_id and e.get("ts", 0) >= started_at]
    actors = [e["actor"] for e in ours]

    print(f"out file : {out_path}")
    print(f"events   : {len(ours)} (actors: {actors})")
    assert len(ours) == 4, f"expected exactly 4 events, got {len(ours)}"
    assert actors.count("client") == 1 and actors.count("dany") == 1 and actors.count("upstream") == 1 and actors.count("parse") == 1

    parse = next(e for e in ours if e["actor"] == "parse")
    print(f"parse    : result={parse['result']!r} issue={parse.get('issue')!r}")
    assert parse["result"].startswith("ok"), f"expected ok:* got {parse['result']}"
    assert "extra_argument" in parse["result"], f"mangle not flagged: {parse['result']}"
    assert "offset" in (parse.get("issue") or ""), f"missing offset in issue: {parse.get('issue')}"

    # --- print the raw NDJSON lines for visual inspection ---
    print(f"\n--- raw NDJSON for request_id={request_id} ---")
    for e in ours:
        line = json.dumps(e, ensure_ascii=False)
        if len(line) > 300:
            line = line[:297] + "..."
        print(" ", line)

    openai_mod.RETRY_BACKOFF_SEC = orig
    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tooltrace.ndjson"
    sys.exit(asyncio.run(main(target)))
