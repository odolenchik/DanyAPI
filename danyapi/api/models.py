from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from ..tokens import estimate_tokens

MODEL_TYPE_BY_NAME = {
    "deepseek-v4-flash": "default",
    "deepseek-v4-pro": "expert",
    "deepseek-v4-vision": "vision",
}

MODEL_NAME_BY_TYPE = {model_type: name for name, model_type in MODEL_TYPE_BY_NAME.items()}

# DeepSeek web search is available on the chat models; vision has no search mode.
SEARCH_CAPABLE_MODEL_TYPES = ("default", "expert")

QWEN_DEFAULT_MODELS = [
    {
        "id": "qwen3.8-max",
        "name": "Qwen3.8-Max",
        "owned_by": "qwen",
        "model_type": "chat",
    },
    {
        "id": "qwen3.7-plus",
        "name": "Qwen3.7-Plus",
        "owned_by": "qwen",
        "model_type": "chat",
    },
    {
        "id": "qwen3.7-max",
        "name": "Qwen3.7-Max",
        "owned_by": "qwen",
        "model_type": "chat",
    },
]


STATUS_TO_FINISH_REASON = {
    "FINISHED": "stop",
    "CONTEXT_LENGTH_EXCEEDED": "length",
    "CONTENT_FILTER": "content_filter",
    "INCOMPLETE": "stop",
    "WIP": "stop",
    "TIMEOUT": "stop",
}

CONTEXT_LENGTH_STATUS = "CONTEXT_LENGTH_EXCEEDED"
INPUT_EXCEEDS_LIMIT = "input_exceeds_limit"
CONTINUE_PROMPT = "Continue"
MAX_CONTINUE_ROUNDS = 5

# The upstream only reports a completed turn by setting response/status to
# FINISHED. A stream that closes without it was cut short (dropped connection,
# upstream restart, silent close) and the client would otherwise receive a
# truncated answer reported as a clean stop.
FINISHED_STATUS = "FINISHED"
# Statuses that mean the turn was cut short rather than produced or refused.
INTERRUPTED_STATUSES = frozenset({"INCOMPLETE", "TIMEOUT"})


class ChatMessage(BaseModel):
    role: str = "user"
    content: Any = ""
    tool_calls: list[Any] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    @field_validator("tool_calls", mode="before")
    @classmethod
    def validate_tool_calls(cls, v: list[Any] | None) -> list[dict[str, Any]] | None:
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("tool_calls must be a list")
        validated_tool_calls = []
        for tool_call in v:
            if not isinstance(tool_call, dict):
                raise ValueError("Each tool_call must be a dictionary")
            if "function" not in tool_call and "name" not in tool_call:
                raise ValueError("Each tool_call must contain 'function' or 'name'")
            validated_tool_calls.append(tool_call)
        return validated_tool_calls


class FileSpec(BaseModel):
    name: str
    content: str
    content_type: str = "application/octet-stream"


class ChatCompletionRequest(BaseModel):
    model: str = Field(default="deepseek-v4-flash")
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    thinking: bool | None = None
    reasoning_effort: str | None = None  # Kimi sends "low"|"medium"|"high"; maps to thinking=True
    search: bool | None = None
    session_id: str | None = None
    # Kimi Code CLI sends a per-session key here (``session_<uuid>``) instead of
    # ``session_id``. It is the only stable per-conversation identifier in its
    # requests, so it doubles as the session id: without it two sessions that
    # open with the same first message share one upstream chat.
    prompt_cache_key: str | None = None
    user: str | None = None
    files: list[FileSpec] | None = None
    tools: list[Any] | None = None
    tool_choice: Any = None
    parallel_tool_calls: bool | None = None
    response_format: Any = None
    stream_options: Any = None


class ImageGenerationRequest(BaseModel):
    model: str = Field(default="qwen-image-2.0-pro")
    prompt: str
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    response_format: str = Field(default="url")
    session_id: str | None = None
    user: str | None = None


def _resolve_model(model: str) -> str:
    model_type = MODEL_TYPE_BY_NAME.get(model)
    if model_type is None:
        raise HTTPException(404, f"Unknown model: {model}")
    return model_type


def _finish_reason(status: Any) -> str:
    if isinstance(status, str):
        return STATUS_TO_FINISH_REASON.get(status, "stop")
    return "stop"


def _deepseek_usage(total: int, prompt: str = "") -> dict:
    value = max(0, int(total or 0))
    prompt_tokens = estimate_tokens(prompt)
    return {"prompt_tokens": prompt_tokens, "completion_tokens": value, "total_tokens": prompt_tokens + value}


def _include_usage(req: ChatCompletionRequest) -> bool:
    opts = getattr(req, "stream_options", None)
    return bool((opts or {}).get("include_usage"))
