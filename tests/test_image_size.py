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
