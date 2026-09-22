from danyapi.deepseek.sse import SSEEvent
from danyapi.qwen import stream as qwen_stream
from danyapi.qwen.stream import QwenStreamReconstructor


def _handle(rec, delta):
    rec.handle(SSEEvent(None, {"choices": [{"delta": delta}]}))


def test_collect_image_urls_empty_text_returns_early():
    rec = QwenStreamReconstructor()
    rec._collect_image_urls("")
    assert rec.image_urls == []


def test_incomplete_tail_truncates_long_window():
    rec = QwenStreamReconstructor()
    tail = "![" + "x" * (qwen_stream._IMAGE_TAIL_LIMIT + 100)
    _handle(rec, {"phase": "image", "content": tail})
    assert len(rec._image_scan_tail) <= qwen_stream._IMAGE_TAIL_LIMIT


def test_incomplete_tail_keeps_unfinished_marker():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "content": "see ![img](https://cdn.qwenlm.ai/a.png) then ![par"})
    assert rec.image_urls == ["https://cdn.qwenlm.ai/a.png"]
    assert rec._image_scan_tail == "![par"


def test_image_url_field_directly_on_delta():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "image_url": "https://cdn.qwenlm.ai/direct.png"})
    assert rec.image_urls == ["https://cdn.qwenlm.ai/direct.png"]


def test_image_field_directly_on_delta():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "image": "https://cdn.qwenlm.ai/alt.png"})
    assert rec.image_urls == ["https://cdn.qwenlm.ai/alt.png"]


def test_extra_non_dict_is_ignored():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "extra": "nope"})
    assert rec.image_urls == []
    assert rec.image_size is None


def test_output_image_hw_valid():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "extra": {"output_image_hw": [[1152, 2048]]}})
    assert rec.image_size == (1152, 2048)


def test_output_image_hw_invalid_numbers():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "extra": {"output_image_hw": [["a", "b"]]}})
    assert rec.image_size is None


def test_output_image_hw_zero_is_ignored():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "extra": {"output_image_hw": [[0, 10]]}})
    assert rec.image_size is None


def test_output_image_hw_short_pair_is_ignored():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "extra": {"output_image_hw": [[1152]]}})
    assert rec.image_size is None


def test_extra_string_image_url():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "extra": {"image_url": "https://cdn.qwenlm.ai/e1.png"}})
    assert rec.image_urls == ["https://cdn.qwenlm.ai/e1.png"]


def test_extra_list_of_strings():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "extra": {"images": ["https://cdn.qwenlm.ai/e2.png", "not-a-url"]}})
    assert rec.image_urls == ["https://cdn.qwenlm.ai/e2.png"]


def test_extra_list_of_dicts():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "extra": {"image_urls": [{"url": "https://cdn.qwenlm.ai/e3.png"}, {"other": 1}]}})
    assert rec.image_urls == ["https://cdn.qwenlm.ai/e3.png"]


def test_extra_url_string():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "extra": {"url": "https://cdn.qwenlm.ai/e4.png"}})
    assert rec.image_urls == ["https://cdn.qwenlm.ai/e4.png"]


def test_duplicate_urls_are_deduped():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "content": "![img](https://cdn.qwenlm.ai/a.png)"})
    _handle(rec, {"phase": "image", "extra": {"image_url": "https://cdn.qwenlm.ai/a.png"}})
    assert rec.image_urls == ["https://cdn.qwenlm.ai/a.png"]


def test_answer_phase_collects_image_urls():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "answer", "content": "![img](https://cdn.qwenlm.ai/answer.png)"})
    assert rec.image_urls == ["https://cdn.qwenlm.ai/answer.png"]
    assert rec.has_content


def test_has_content_true_for_image_only():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "image_url": "https://cdn.qwenlm.ai/only.png"})
    assert rec.content == ""
    assert rec.has_content


def test_trailing_punctuation_stripped_from_url():
    rec = QwenStreamReconstructor()
    _handle(rec, {"phase": "image", "content": "see ![img](https://cdn.qwenlm.ai/a.png)."})
    assert rec.image_urls == ["https://cdn.qwenlm.ai/a.png"]
