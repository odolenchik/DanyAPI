import base64
from io import BytesIO

import pytest
from fastapi import HTTPException

from danyapi.api.openai import _aspect_ratio, _parse_image_size, _resize_image_bytes


def _png_bytes(width, height):
    from PIL import Image

    img = Image.new("RGB", (width, height), color=(10, 20, 30))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_parse_size_none():
    assert _parse_image_size(None) is None


def test_parse_size_empty():
    assert _parse_image_size("") is None
    assert _parse_image_size("   ") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1152*2048", (1152, 2048)),
        ("1152x2048", (1152, 2048)),
        ("1024X1024", (1024, 1024)),
        (" 512*512 ", (512, 512)),
        ("256,256", (256, 256)),
        ("256\u00d7256", (256, 256)),
    ],
)
def test_parse_size_formats(raw, expected):
    assert _parse_image_size(raw) == expected


def test_parse_size_invalid_raises_400():
    with pytest.raises(HTTPException) as excinfo:
        _parse_image_size("big")
    assert excinfo.value.status_code == 400


def test_parse_size_single_dimension_raises_400():
    with pytest.raises(HTTPException):
        _parse_image_size("1152")


def test_parse_size_out_of_range_raises_400():
    with pytest.raises(HTTPException):
        _parse_image_size("8*8")
    with pytest.raises(HTTPException):
        _parse_image_size("99999*100")


def test_resize_none_passthrough():
    data = b"raw-bytes"
    assert _resize_image_bytes(data, None) == data


def test_resize_applies_dimensions():
    original = _png_bytes(2048, 2048)
    resized = _resize_image_bytes(original, (1152, 2048))
    from PIL import Image

    with Image.open(BytesIO(resized)) as img:
        assert img.size == (1152, 2048)


def test_resize_roundtrip_b64():
    original = _png_bytes(64, 64)
    payload = _resize_image_bytes(original, (32, 16))
    encoded = base64.b64encode(payload).decode()
    from PIL import Image

    with Image.open(BytesIO(base64.b64decode(encoded))) as img:
        assert img.size == (32, 16)


def test_resize_invalid_source_returns_original():
    garbage = b"not-an-image"
    assert _resize_image_bytes(garbage, (10, 10)) == garbage


def test_resize_converts_cmyk_jpeg_to_rgb():
    from PIL import Image

    img = Image.new("CMYK", (16, 16))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    resized = _resize_image_bytes(buf.getvalue(), (8, 8))
    with Image.open(BytesIO(resized)) as out:
        assert out.size == (8, 8)
        assert out.mode in ("RGB", "L")


def _img_uri(tag: str) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(tag.encode()).decode()


def test_collect_attachments_skips_history_already_sent():
    """With a live session, only the current turn's attachments need uploading.

    A client resends the whole conversation, so the same image would otherwise be
    re-uploaded on every turn even though the upstream session already holds it.
    """
    from types import SimpleNamespace

    from danyapi.api.attachments import _collect_attachments

    def msg(role, content):
        return SimpleNamespace(role=role, content=content)

    old_image = [{"type": "image_url", "image_url": {"url": _img_uri("OLD")}}]
    new_image = [{"type": "image_url", "image_url": {"url": _img_uri("NEW")}}]
    req = SimpleNamespace(
        messages=[msg("user", old_image), msg("assistant", "seen"), msg("user", new_image)],
        files=None,
    )

    # Without a session every attachment is new and must be uploaded.
    assert len(_collect_attachments(req)) == 2
    # With a live session only the turn after the last assistant reply counts.
    fresh = _collect_attachments(req, skip_sent_history=True)
    assert len(fresh) == 1
    assert fresh[0].data == b"NEW"


def test_collect_attachments_skip_history_with_no_assistant_keeps_all():
    from types import SimpleNamespace

    from danyapi.api.attachments import _collect_attachments

    image = [{"type": "image_url", "image_url": {"url": _img_uri("ONLY")}}]
    req = SimpleNamespace(messages=[SimpleNamespace(role="user", content=image)], files=None)
    assert len(_collect_attachments(req, skip_sent_history=True)) == 1


def test_content_fingerprint_hashes_image_instead_of_copying_it():
    """An image data URI must be fingerprinted by digest, not embedded whole.

    Embedding it produced an 11 MB string on every request that carried one.
    The digest keeps the same image stable across calls without the copy.
    """
    from danyapi.tools.prompt import _content_fingerprint

    uri = "data:image/png;base64," + "A" * 20000
    msg = [{"type": "text", "text": "describe"}, {"type": "image_url", "image_url": {"url": uri}}]

    fp = _content_fingerprint(msg)
    assert "describe" in fp
    assert "image:" in fp
    # The bulky URI itself must not be reproduced.
    assert uri not in fp
    assert len(fp) < 200
    # Same image -> same fingerprint; different image -> different one.
    assert _content_fingerprint(msg) == fp
    other = [{"type": "image_url", "image_url": {"url": uri + "B"}}]
    assert _content_fingerprint(other) != fp


def test_content_fingerprint_accepts_plain_string_image_url():
    from danyapi.tools.prompt import _content_fingerprint

    uri = "data:image/png;base64,QUJD"
    as_str = [{"type": "image_url", "image_url": uri}]
    as_dict = [{"type": "image_url", "image_url": {"url": uri}}]
    assert _content_fingerprint(as_str) == _content_fingerprint(as_dict)


@pytest.mark.parametrize(
    ("dims", "expected"),
    [
        ((1024, 1024), "1:1"),
        ((512, 512), "1:1"),
        ((1152, 2048), "9:16"),
        ((2048, 1152), "16:9"),
        ((1920, 1080), "16:9"),
        ((1080, 1920), "9:16"),
        ((300, 200), "4:3"),
        ((200, 300), "3:4"),
        ((1000, 100), "16:9"),
        ((100, 1000), "9:16"),
        ((900, 1600), "9:16"),
    ],
)
def test_aspect_ratio_picks_nearest(dims, expected):
    assert _aspect_ratio(dims) == expected


def test_aspect_ratio_is_stable_for_ratio_shaped_input():
    assert _aspect_ratio(_parse_image_size("1152x2048")) == "9:16"
