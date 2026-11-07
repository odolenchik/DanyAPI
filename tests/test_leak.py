"""The sanitizer must strip real tool-call markup and leave prose alone.

Real traces contain two very different things that both look like "markup in
the text": a mangled tag fragment left over from a broken call, and prose that
*discusses* the format ("no invoke tags, no wrappers"). Cutting everything with
an angle bracket would ruin the second kind, so the rules are narrow and the
cases below come from the actual trace files.

Tags are assembled from parts rather than written literally: a string that
mixes Cyrillic prose with a Latin tag in one literal trips RUF001, and the
editor tooling truncates content on literal closing-tag sequences.
"""

import pytest

from danyapi.tools.leak import has_tool_markup, sanitize_tool_markup

LT = "<"
GT = ">"
BAR = "\uff5c\uff5c"


def tag_open(name: str, attrs: str = "") -> str:
    return f"{LT}{BAR}DSML{BAR}{name}{attrs}{GT}"


def tag_close(name: str) -> str:
    return f"{LT}/{BAR}DSML{BAR}{name}{GT}"


def plain_close(name: str) -> str:
    return f"{LT}/{name}{GT}"


DSML_OPEN = tag_open("invoke", ' name="Read"')
DSML_PARAM = tag_open("parameter", ' name="path" string="true"')


# --- prose that mentions the format: must survive untouched -----------------


PROSE = [
    "Формат: без тегов и без обёрток, только JSON.",
    "A plain English answer with no markup at all.",
    "Слово tool_calls встречается в обычном тексте.",
]


@pytest.mark.parametrize("text", PROSE)
def test_prose_is_untouched(text):
    assert sanitize_tool_markup(text) == text
    assert has_tool_markup(text) is False


def test_inline_code_example_is_preserved():
    text = "Пиши так: " + "`" + tag_open("invoke") + "`" + " и всё."
    assert sanitize_tool_markup(text) == text
    assert has_tool_markup(text) is False


def test_fenced_example_is_preserved():
    fence = "```"
    text = "Example:" + fence + "\n" + DSML_OPEN + "\n" + fence + "\nend."
    assert sanitize_tool_markup(text) == text
    assert has_tool_markup(text) is False


# --- real markup: stripped --------------------------------------------------


def test_dsml_tag_removed_text_kept():
    cleaned = sanitize_tool_markup("Ответ\n" + DSML_OPEN)
    assert cleaned == "Ответ"


def test_parameter_tag_removed_value_kept():
    cleaned = sanitize_tool_markup("Путь:\n" + DSML_PARAM + "value.txt" + tag_close("parameter"))
    assert cleaned == "Путь:\nvalue.txt"


def test_orphan_fragments_removed():
    text = "Готово." + plain_close("parameter") + " invoke" + GT + " " + plain_close("tool_result")
    cleaned = sanitize_tool_markup(text)
    assert cleaned == "Готово."


def test_bare_dsml_close_removed():
    assert sanitize_tool_markup("хвост" + plain_close("DSML")) == "хвост"


def test_markup_only_reply_becomes_empty():
    """Signals the caller that the whole answer was a broken call."""
    assert sanitize_tool_markup(plain_close("tool_calls")) == ""


def test_prose_before_markup_survives():
    """The whole point: junk around real text goes, the text stays."""
    text = "Этап закрыт целиком. Коммичу. Tool result: 20 files changed" + plain_close("parameter") + " invoke" + GT + " " + plain_close("tool_result")
    cleaned = sanitize_tool_markup(text)
    assert "Этап закрыт целиком" in cleaned
    assert "20 files changed" in cleaned
    assert "parameter" + GT not in cleaned
    assert "tool_result" + GT not in cleaned


def test_broken_dsml_marker_removed():
    """A marker mangled by replacement characters still has to go."""
    text = "Хвост ответа: 300 " + LT + "\uff5c\ufffd\ufffdDSML\uff5c\uff5ctool_calls" + GT
    cleaned = sanitize_tool_markup(text)
    assert "Хвост ответа" in cleaned
    assert "DSML" not in cleaned
    assert has_tool_markup(cleaned) is False


def test_idempotent():
    text = "Готово." + plain_close("parameter") + " invoke" + GT
    once = sanitize_tool_markup(text)
    assert sanitize_tool_markup(once) == once


def test_empty_input():
    assert sanitize_tool_markup("") == ""
    assert has_tool_markup("") is False


# --- has_tool_markup: the "nothing but a broken call" signal ----------------


def test_has_tool_markup_flags_surviving_tags():
    assert has_tool_markup(tag_open("tool_calls")) is True
    assert has_tool_markup(plain_close("invoke")) is True
    assert has_tool_markup("stray invoke" + GT + " here") is True


def test_has_tool_markup_ignores_code_spans():
    assert has_tool_markup("текст `" + tag_open("invoke") + "` текст") is False
