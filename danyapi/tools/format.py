from __future__ import annotations

from .prompt import ToolCall


def limit_tool_calls(tool_calls: list[ToolCall]) -> list[ToolCall]:
    """Keep only the first call of a reply.

    Used in single-call mode. The prompt already asks for one call, but the model
    can still batch several; dropping the extras keeps the contract ("one call
    per reply") true for the client, which then issues the next call itself. The
    dropped calls are not lost work: the model sees the first result next round
    and can re-issue what is still needed.
    """
    return tool_calls[:1]


def format_tool_message(tool_calls: list[ToolCall], text: str, reasoning: str | None = None) -> dict:
    message: dict = {"role": "assistant", "content": text}
    message["tool_calls"] = [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
        }
        for call in tool_calls
    ]
    if reasoning:
        message["reasoning_content"] = reasoning
    return message


def tool_call_deltas(tool_calls: list[ToolCall], text: str | None = None) -> list[dict]:
    deltas: list[dict] = []
    if text:
        deltas.append({"role": "assistant", "content": text})
    for index, call in enumerate(tool_calls):
        deltas.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": index,
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": ""},
                    }
                ],
            }
        )
        arguments = call.arguments
        if arguments:
            step = max(1, (len(arguments) + 5) // 6)
            for offset in range(0, len(arguments), step):
                deltas.append({"tool_calls": [{"index": index, "function": {"arguments": arguments[offset : offset + step]}}]})
    return deltas
