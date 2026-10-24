"""Detect when the web front-ends change under the clients.

DeepSeek and Qwen are driven through their web APIs, not a published
contract: the endpoints, headers and front-end version are read off the
site and encoded in ``danyapi/deepseek/client.py`` and
``danyapi/qwen/client.py``. When the site is redeployed those encodings go
stale silently — the process keeps starting, and the first symptom is a 401
or a 405 hours later.

These probes are cheap, need no credentials and answer one question: is the
endpoint shape still the one the client expects? Run them after a deploy or
on a timer:

    python scripts/platform_drift.py deepseek
    python scripts/platform_drift.py qwen
    python scripts/platform_drift.py all --json

Exit code is 0 when every probe matches, 1 on drift or a network failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The probes exist to notice drift, so they import the very constants they
# guard: a stale constant is exactly what should show up as a mismatch.
from danyapi.deepseek.client import BASE_URL as DEEPSEEK_URL  # noqa: E402
from danyapi.deepseek.client import CLIENT_HEADERS as DEEPSEEK_HEADERS  # noqa: E402
from danyapi.deepseek.client import USER_AGENT as DEEPSEEK_UA  # noqa: E402
from danyapi.qwen.client import AUTH_URL as QWEN_AUTH_URL  # noqa: E402
from danyapi.qwen.client import USER_AGENT as QWEN_UA  # noqa: E402
from danyapi.qwen.client import WEB_VERSION as QWEN_WEB_VERSION  # noqa: E402

PROBE_TIMEOUT_SEC = 20.0
#: Content type prefix a live JSON endpoint answers with. The SPA catch-all
#: returns HTML, which is how a removed path looks from the outside.
JSON_TYPE = "application/json"


@dataclass
class Probe:
    """One check: a name, whether it matched, and what was seen."""

    name: str
    ok: bool
    detail: str
    extra: dict[str, object] = field(default_factory=dict)


def _content_type(resp: httpx.Response) -> str:
    return resp.headers.get("content-type", "").split(";")[0].strip()


def _is_json(resp: httpx.Response) -> bool:
    return _content_type(resp) == JSON_TYPE


def probe_deepseek(client: httpx.Client) -> list[Probe]:
    """DeepSeek: the PoW and settings endpoints still answer JSON.

    A removed path is not a 404 here — the site serves its SPA shell with
    ``200 text/html`` instead, so the content type is the real signal.
    """
    probes: list[Probe] = []

    resp = client.post(
        f"{DEEPSEEK_URL}/api/v0/chat/create_pow_challenge",
        json={"target_path": "/api/v0/chat/completion"},
    )
    body = resp.json() if _is_json(resp) else {}
    # Without a token the endpoint answers 40002 "Missing Token"; the point is
    # that it answers at all and in the JSON envelope the client parses.
    ok = _is_json(resp) and body.get("code") in (0, 40002, 40003)
    probes.append(
        Probe(
            "deepseek pow challenge",
            ok,
            f"http={resp.status_code} type={_content_type(resp)} code={body.get('code')}",
        )
    )

    resp = client.get(f"{DEEPSEEK_URL}/api/v0/client/settings")
    body = resp.json() if _is_json(resp) else {}
    ok = _is_json(resp) and body.get("code") == 0
    probes.append(
        Probe(
            "deepseek client settings",
            ok,
            f"http={resp.status_code} type={_content_type(resp)} code={body.get('code')}",
        )
    )

    # The client stamps every request with these headers; a new bundle version
    # is the usual reason a working token starts being refused.
    probes.append(
        Probe(
            "deepseek client headers",
            bool(DEEPSEEK_HEADERS.get("x-client-version")),
            f"x-client-version={DEEPSEEK_HEADERS.get('x-client-version')}",
            {"user_agent": DEEPSEEK_UA},
        )
    )
    return probes


def probe_qwen(client: httpx.Client) -> list[Probe]:
    """Qwen: the auth refresh endpoint still answers its JSON envelope.

    Called with a bogus credential on purpose: a refusal is the expected
    answer and proves the path, the headers and ``WEB_VERSION`` are still
    accepted. Only the response shape matters here.
    """
    probes: list[Probe] = []
    headers = {
        "User-Agent": QWEN_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://chat.qwen.ai/",
        "source": "web",
        "version": QWEN_WEB_VERSION,
        "x-request-origin": "https://chat.qwen.ai",
        "Timezone": "Sat Oct 24 2026 17:00:00 GMT+0300 (Moscow Standard Time)",
    }
    # The credential rides on a cookie, set on the client rather than the
    # request: httpx deprecates per-request cookies, and the value is a probe.
    client.cookies.set("refresh_token", "probe.invalid.token", domain="auth.qwen.ai")
    try:
        resp = client.get(f"{QWEN_AUTH_URL}/api/v2/auths/refresh", headers=headers)
    finally:
        client.cookies.delete("refresh_token", domain="auth.qwen.ai")
    body = resp.json() if _is_json(resp) else {}
    data = body.get("data") or {}
    # A live endpoint answers {"success": false, "data": {"code": "Unauthorized"}}.
    # A 405 with HTML means the header set — version included — is no longer
    # accepted, which is the drift worth reporting.
    ok = _is_json(resp) and "success" in body
    probes.append(
        Probe(
            "qwen auth refresh",
            ok,
            f"http={resp.status_code} type={_content_type(resp)} "
            f"success={body.get('success')} code={data.get('code')}",
        )
    )
    probes.append(
        Probe(
            "qwen web version",
            bool(QWEN_WEB_VERSION),
            f"version={QWEN_WEB_VERSION}",
        )
    )
    return probes


PROBES = {
    "deepseek": probe_deepseek,
    "qwen": probe_qwen,
}


def run(names: list[str]) -> list[Probe]:
    results: list[Probe] = []
    with httpx.Client(timeout=PROBE_TIMEOUT_SEC, follow_redirects=True) as client:
        for name in names:
            try:
                results.extend(PROBES[name](client))
            except httpx.HTTPError as exc:
                results.append(Probe(f"{name}: network", False, f"{type(exc).__name__}: {exc}"))
    return results


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Detect web API drift for DanyAPI providers")
    parser.add_argument("platform", choices=[*PROBES, "all"])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv[1:])

    names = list(PROBES) if args.platform == "all" else [args.platform]
    results = run(names)
    failed = [probe for probe in results if not probe.ok]

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not failed,
                    "probes": [
                        {"name": p.name, "ok": p.ok, "detail": p.detail, **p.extra}
                        for p in results
                    ],
                },
                ensure_ascii=False,
            )
        )
    else:
        for probe in results:
            mark = "OK  " if probe.ok else "DRIFT"
            print(f"  [{mark}] {probe.name}: {probe.detail}")
        print("no drift" if not failed else f"drift in {len(failed)} probe(s)")

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
