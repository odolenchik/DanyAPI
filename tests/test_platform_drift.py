"""Drift probes: the response shapes they treat as healthy and as drift."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    """Import the script by path.

    It is registered in ``sys.modules`` before execution because ``dataclass``
    resolves the module of the class it decorates.
    """
    name = "platform_drift"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "platform_drift.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


drift = _load()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_deepseek_ok_when_endpoints_answer_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/create_pow_challenge"):
            return httpx.Response(200, json={"code": 40002, "msg": "Missing Token"})
        return httpx.Response(200, json={"code": 0, "msg": "", "data": {}})

    probes = drift.probe_deepseek(_client(handler))

    assert [p.name for p in probes] == [
        "deepseek pow challenge",
        "deepseek client settings",
        "deepseek client headers",
    ]
    assert all(p.ok for p in probes)


def test_deepseek_drift_when_spa_shell_replaces_json() -> None:
    """A removed path answers 200 text/html with the SPA shell, not a 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<!doctype html><html></html>")

    probes = drift.probe_deepseek(_client(handler))

    assert not any(p.ok for p in probes if p.name != "deepseek client headers")


def test_qwen_ok_on_expected_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": False, "data": {"code": "Unauthorized"}}
        )

    probes = drift.probe_qwen(_client(handler))

    assert [p.name for p in probes] == ["qwen auth refresh", "qwen web version"]
    assert all(p.ok for p in probes)


def test_qwen_drift_on_405_html() -> None:
    """A rejected header set — the version included — comes back as HTML."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(405, text="<html>405</html>")

    probes = drift.probe_qwen(_client(handler))
    auth = next(p for p in probes if p.name == "qwen auth refresh")

    assert auth.ok is False
    assert "405" in auth.detail


def test_qwen_probe_sends_the_configured_version() -> None:
    """The probe must exercise WEB_VERSION, not a copy of it."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"success": False, "data": {}})

    drift.probe_qwen(_client(handler))

    assert seen["version"] == drift.QWEN_WEB_VERSION


def test_run_reports_network_failure_as_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(client: httpx.Client) -> list:
        raise httpx.ConnectError("no route")

    monkeypatch.setitem(drift.PROBES, "qwen", boom)

    probes = drift.run(["qwen"])

    assert len(probes) == 1
    assert probes[0].ok is False
    assert "ConnectError" in probes[0].detail


def test_main_exit_code_reflects_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        drift, "run", lambda names: [drift.Probe("x", False, "broken")]
    )
    assert drift.main(["prog", "all"]) == 1

    monkeypatch.setattr(drift, "run", lambda names: [drift.Probe("x", True, "fine")])
    assert drift.main(["prog", "all"]) == 0


def test_main_json_output(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(
        drift, "run", lambda names: [drift.Probe("x", True, "fine", {"v": "1"})]
    )

    drift.main(["prog", "all", "--json"])

    payload = capsys.readouterr().out.strip()
    assert '"ok": true' in payload
    assert '"v": "1"' in payload
