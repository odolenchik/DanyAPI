"""Strip tool-call markup from text that is about to be shown to the client.

When the model answers with a tool call, the call travels in its own channel
(``tool_calls``) and the prose travels as ``content``. Sometimes the split
fails: an unparsed reply is forwarded verbatim, and a parsed one can carry
leftover tag fragments at the tail. The client then sees raw DSML.

Removing every angle bracket is wrong: a reply may legitimately *discuss* the
format. Real traces contain prose like "без тегов `<invoke>`, без обёрток" and
fenced examples of correct calls. So the sanitizer only touches code-free
segments and only removes tags that look structural: a DSML marker with its
separator, or a tool-call element carrying attributes or a closing slash.
Inline code and fenced blocks are copied through untouched.
"""

from __future__ import annotations

import re

# A fenced block or an inline code span: copied verbatim, never sanitized.
_PROTECTED = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)

# The marker itself is what makes a DSML tag structural. Both spellings occur:
# with the separator the prompt asks for, and bare, which the model emits when
# it mangles a closing tag.
_DSML_TAG = re.compile(
    r"<\s*/?\s*(?:(?:\|\||\uff5c\uff5c)\s*DSML\b[^>]{0,200}|DSML\s*)>",
    re.IGNORECASE,
)

# Bare elements. Attributes or a closing slash are required, so the words
# "tool_calls" or "invoke" mentioned inside a sentence stay untouched.
_STRUCT_TAG = re.compile(
    r"<\s*(?P<slash>/?)\s*(?:tool_calls|function_calls|invoke|parameter|tool_result)\b"
    r"(?P<rest>[^>]{0,200})>",
    re.IGNORECASE,
)

# Fragments left after a mangled tag loses its opening bracket: the model emits
# a stray "invoke>" or "tool_calls>" with no angle bracket in front. The
# lookbehind keeps ordinary words ("invoke the tool") out of it.
_ORPHAN_TAG = re.compile(
    r"(?<![\w<])(?:tool_calls|function_calls|invoke|parameter|tool_result)\s*>",
    re.IGNORECASE,
)

# A DSML marker broken by a replacement character or a lost separator, e.g.
# "<|??DSML||tool_calls>" or "<| tool_calls>".
_BROKEN_DSML = re.compile(r"<\s*[\uff5c|]\s*[^\s<>]{0,4}?\s*DSML\b[^>]{0,120}>", re.IGNORECASE)
_BROKEN_MARKER = re.compile(r"<\s*[\uff5c|]\s*(?:tool_calls|invoke|parameter)\s*>", re.IGNORECASE)

_TRAILING_BLANKS = re.compile(r"[ \t]+$", re.MULTILINE)
_EXTRA_BLANK_LINES = re.compile(r"\n{3,}")


def _is_structural(match: re.Match[str]) -> bool:
    """A closing slash or an attribute makes an element a real tag."""
    return bool(match.group("slash") or match.group("rest").strip())


def _strip_tags(segment: str) -> str:
    if not segment:
        return segment

    def replace(match: re.Match[str]) -> str:
        return "" if _is_structural(match) else match.group(0)

    segment = _DSML_TAG.sub("", segment)
    segment = _BROKEN_DSML.sub("", segment)
    segment = _BROKEN_MARKER.sub("", segment)
    segment = _STRUCT_TAG.sub(replace, segment)
    return _ORPHAN_TAG.sub("", segment)


def _segments(text: str):
    """Yield ``(segment, protected)`` pairs over the text."""
    pos = 0
    for match in _PROTECTED.finditer(text):
        yield text[pos : match.start()], False
        yield match.group(0), True
        pos = match.end()
    yield text[pos:], False


def sanitize_tool_markup(text: str) -> str:
    """Remove structural tool-call tags, keeping the prose around them.

    Returns the input unchanged when it carries no structural tag, so callers
    can apply this unconditionally without rewriting ordinary replies.
    """
    if not text:
        return text

    out: list[str] = []
    for segment, protected in _segments(text):
        out.append(segment if protected else _strip_tags(segment))
    cleaned = "".join(out)

    if cleaned == text:
        return text
    cleaned = _TRAILING_BLANKS.sub("", cleaned)
    cleaned = _EXTRA_BLANK_LINES.sub("\n\n", cleaned)
    return cleaned.strip()


def has_tool_markup(text: str) -> bool:
    """Whether structural markup survives outside code spans.

    A reply that was nothing but a broken call must not reach the client as an
    empty message, so callers use this to tell "the markup was the whole
    answer" from "the markup was junk around real prose".
    """
    if not text:
        return False
    for segment, protected in _segments(text):
        if protected:
            continue
        if _DSML_TAG.search(segment) or _BROKEN_DSML.search(segment) or _BROKEN_MARKER.search(segment):
            return True
        if any(_is_structural(m) for m in _STRUCT_TAG.finditer(segment)):
            return True
        if _ORPHAN_TAG.search(segment):
            return True
    return False
