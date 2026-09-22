"""Unit tests for pydantic request models in danyapi.api.models."""

import pytest
from pydantic import ValidationError

from danyapi.api.models import ChatMessage


def test_chat_message_defaults():
    msg = ChatMessage()
    assert msg.role == "user"
    assert msg.content == ""
    assert msg.tool_calls is None
    assert msg.tool_call_id is None
    assert msg.name is None


def test_tool_calls_none_passes_through():
    msg = ChatMessage(role="assistant", tool_calls=None)
    assert msg.tool_calls is None


def test_tool_calls_accepts_function_shape():
    msg = ChatMessage(
        role="assistant",
        tool_calls=[{"function": {"name": "get_weather", "arguments": "{}"}}],
    )
    assert msg.tool_calls == [{"function": {"name": "get_weather", "arguments": "{}"}}]


def test_tool_calls_accepts_name_shape():
    msg = ChatMessage(role="assistant", tool_calls=[{"name": "get_weather"}])
    assert msg.tool_calls == [{"name": "get_weather"}]


def test_tool_calls_rejects_non_list():
    with pytest.raises(ValidationError, match="tool_calls must be a list"):
        ChatMessage(role="assistant", tool_calls={"function": {}})


def test_tool_calls_rejects_non_dict_entry():
    with pytest.raises(ValidationError, match="Each tool_call must be a dictionary"):
        ChatMessage(role="assistant", tool_calls=["not-a-dict"])


def test_tool_calls_rejects_entry_without_function_or_name():
    with pytest.raises(ValidationError, match="must contain 'function' or 'name'"):
        ChatMessage(role="assistant", tool_calls=[{"index": 0}])
