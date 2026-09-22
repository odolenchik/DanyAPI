from __future__ import annotations

import re

from ..deepseek.sse import SSEEvent

ANSWER_PHASES = {"answer", "deep_research_answer", "ReportGeneration", "PdfMdGen"}
IMAGE_PHASES = {"image", "image_generation", "image_gen", "t2i"}
THINK_PHASES = {"think", "DeepThinking"}
SUMMARY_PHASE = "thinking_summary"

_IMAGE_URL_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)>'\"]+)\)|(https?://cdn\.qwenlm\.ai/[^\s)>'\"]+)")
_IMAGE_TAIL_LIMIT = 4096
_TRAILING_PUNCT = ".,;:!?"


def _delta_text(delta: dict, key: str) -> str:
    value = delta.get(key)
    return value if isinstance(value, str) else ""


def _extract_image_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in _IMAGE_URL_RE.finditer(text):
        url = (match.group(1) or match.group(2) or "").rstrip(_TRAILING_PUNCT)
        if url:
            urls.append(url)
    return urls


def _incomplete_tail(window: str) -> str:
    if len(window) > _IMAGE_TAIL_LIMIT:
        window = window[-_IMAGE_TAIL_LIMIT:]
    scan_from = 0
    for match in _IMAGE_URL_RE.finditer(window):
        scan_from = match.end()
    tail_index = len(window)
    for marker in ("![", "http"):
        start = window.rfind(marker, scan_from)
        if start != -1 and start < tail_index:
            tail_index = start
    return window[tail_index:] if tail_index < len(window) else ""


class QwenStreamReconstructor:
    def __init__(self) -> None:
        self.response_id: str | None = None
        self.content: str = ""
        self.reasoning: str = ""
        self.finished: bool = False
        self.error: dict | None = None
        self.usage: dict = {}
        self._prev_content: str = ""
        self._prev_reasoning: str = ""
        self._summary_mode: bool = False
        self.image_urls: list[str] = []
        self.image_size: tuple[int, int] | None = None
        self._image_scan_tail: str = ""
        self._seen_image_urls: set[str] = set()

    def _add_image_url(self, url: str) -> None:
        if url.startswith("http") and url not in self._seen_image_urls:
            self._seen_image_urls.add(url)
            self.image_urls.append(url)

    def _collect_image_urls(self, text: str) -> None:
        if not text:
            return
        window = self._image_scan_tail + text
        for url in _extract_image_urls(window):
            self._add_image_url(url)
        self._image_scan_tail = _incomplete_tail(window)

    def _apply_image_delta(self, delta: dict) -> None:
        text = _delta_text(delta, "content")
        if text:
            self.content += text
            self._collect_image_urls(text)
        image_field = delta.get("image_url") or delta.get("image")
        if isinstance(image_field, str):
            self._add_image_url(image_field)
        extra = delta.get("extra")
        if not isinstance(extra, dict):
            return
        hw = extra.get("output_image_hw")
        if isinstance(hw, list) and hw:
            pair = hw[0]
            if isinstance(pair, list) and len(pair) >= 2:
                try:
                    width, height = int(pair[0]), int(pair[1])
                except (TypeError, ValueError):
                    width = height = 0
                if width > 0 and height > 0:
                    self.image_size = (width, height)
        for key in ("image_url", "image_urls", "images", "url"):
            val = extra.get(key)
            if isinstance(val, str):
                self._add_image_url(val)
            elif isinstance(val, list):
                for item in val:
                    item_url = item if isinstance(item, str) else (item.get("url") if isinstance(item, dict) else None)
                    if isinstance(item_url, str):
                        self._add_image_url(item_url)

    def handle(self, event: SSEEvent) -> None:
        data = event.data
        if not isinstance(data, dict):
            return
        created = data.get("response.created")
        if isinstance(created, dict) and created.get("response_id"):
            self.response_id = created["response_id"]
        if data.get("response.stopped") is not None:
            self.finished = True
        if data.get("done"):
            self.finished = True
        if data.get("response_id"):
            self.response_id = data["response_id"]
        if data.get("error"):
            error = data["error"]
            self.error = error if isinstance(error, dict) else {"code": "Internal_Server_Error", "details": error}
            return
        if isinstance(data.get("usage"), dict):
            self.usage = data["usage"]
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            return
        if delta.get("status") == "finished":
            self.finished = True
        phase = delta.get("phase") or ""
        if phase in IMAGE_PHASES:
            self._apply_image_delta(delta)
        elif phase in ANSWER_PHASES:
            text = _delta_text(delta, "content")
            if text:
                self.content += text
                self._collect_image_urls(text)
        elif phase in THINK_PHASES:
            text = _delta_text(delta, "content")
            if text:
                self.reasoning += text
                self._summary_mode = False
        elif phase == SUMMARY_PHASE:
            self._summary_mode = True
            extra = delta.get("extra")
            if isinstance(extra, dict):
                summary = extra.get("summary_thought")
                if isinstance(summary, dict):
                    items = summary.get("content")
                    if isinstance(items, list):
                        joined = "\n\n".join(str(item) for item in items if item)
                        if joined:
                            self.reasoning = joined

    def take_diffs(self) -> tuple[str, str]:
        content, reasoning = self.content, self.reasoning
        c_diff = content.removeprefix(self._prev_content)
        r_diff = reasoning.removeprefix(self._prev_reasoning)
        self._prev_content, self._prev_reasoning = content, reasoning
        return c_diff, r_diff

    @property
    def has_content(self) -> bool:
        return bool(self.content or self.reasoning or self.image_urls)

    @property
    def usage_tokens(self) -> dict:
        return {
            "prompt_tokens": self.usage.get("input_tokens", 0),
            "completion_tokens": self.usage.get("output_tokens", 0),
            "total_tokens": self.usage.get("total_tokens", 0),
        }


def error_code(error: dict | None) -> str | None:
    if not error:
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None
