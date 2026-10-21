"""Repeat guard: a call repeated round after round must be stopped."""

from __future__ import annotations

from types import SimpleNamespace

from danyapi.repeat import call_signature, note_calls, reset


def _call(name: str, arguments: str):
    return SimpleNamespace(name=name, arguments=arguments)


def _session(**extra):
    return SimpleNamespace(extra=dict(extra))


def test_signature_normalises_key_order():
    a = call_signature([_call("Bash", '{"command": "ls", "timeout": 5}')])
    b = call_signature([_call("Bash", '{"timeout": 5, "command": "ls"}')])
    assert a is not None and a == b


def test_signature_differs_for_different_arguments():
    a = call_signature([_call("Bash", '{"command": "ls"}')])
    b = call_signature([_call("Bash", '{"command": "pwd"}')])
    assert a != b


def test_signature_none_without_calls():
    assert call_signature([]) is None
    assert call_signature(None) is None
    assert call_signature([SimpleNamespace(name="", arguments="{}")]) is None


def test_repeat_hits_threshold_and_resets_on_change():
    session = _session()
    call = [_call("Read", '{"path": "/a"}')]
    assert [note_calls(session, call, 3) for _ in range(3)] == [False, False, True]
    # A different call starts a fresh run.
    assert note_calls(session, [_call("Read", '{"path": "/b"}')], 3) is False


def test_repeat_count_persists_across_calls():
    session = _session()
    call = [_call("WaitFor", '{"task_id": "t1"}')]
    for _ in range(2):
        note_calls(session, call, 5)
    assert session.extra["repeat_guard"]["count"] == 2


def test_reset_clears_state():
    session = _session()
    call = [_call("Bash", '{"command": "ls"}')]
    note_calls(session, call, 5)
    reset(session)
    assert "repeat_guard" not in session.extra
    assert note_calls(session, call, 5) is False


def test_no_state_means_no_abort():
    """A session that cannot hold state must never trip the guard."""
    assert note_calls(SimpleNamespace(), [_call("Bash", "{}")], 2) is False
    reset(SimpleNamespace())  # must not raise


def test_signature_accepts_non_string_arguments():
    """Arguments may arrive as a dict; both spellings must normalise the same."""
    a = call_signature([SimpleNamespace(name="Bash", arguments={"command": "ls", "timeout": 5})])
    b = call_signature([_call("Bash", '{"timeout": 5, "command": "ls"}')])
    assert a is not None and a == b


def test_signature_keeps_unparsable_arguments_as_is():
    """A non-JSON argument string still yields a stable signature."""
    assert call_signature([_call("Bash", "not json")]) == "Bash:not json"


def test_note_calls_clears_state_without_calls():
    """A round with no calls ends the repeat run instead of keeping the count."""
    session = _session()
    note_calls(session, [_call("Bash", '{"command": "ls"}')], 5)
    assert note_calls(session, [], 5) is False
    assert "repeat_guard" not in session.extra


def test_note_calls_tolerates_bad_count():
    """A corrupted counter must not crash the guard."""
    session = _session(repeat_guard={"signature": 'Bash:{"command": "ls"}', "count": "x"})
    call = [_call("Bash", '{"command": "ls"}')]
    assert note_calls(session, call, 5) is False
    assert session.extra["repeat_guard"]["count"] == 2
