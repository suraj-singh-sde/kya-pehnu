import io
import subprocess
import sys

import pytest
from PIL import Image, UnidentifiedImageError

from kyapehnu.core.errors import HeicExtraMissingError, ImageTooLargeError
from kyapehnu.files.local import LocalStore
from kyapehnu.wardrobe.ingest import images
from tests.conftest import noise_image_bytes


def _write(tmp_path, name, im, fmt, **kw):
    """Store the picture as the owner's original and return its key."""
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    key = f"images/1/original/{name}"
    LocalStore(tmp_path).put(key, buf.getvalue())
    return key


def test_load_applies_exif_orientation_and_resizes(tmp_path):
    im = Image.new("RGB", (4000, 2000), "red")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise on display
    key = _write(tmp_path, "a.jpg", im, "JPEG", exif=exif)
    out = images.load_normalized(LocalStore(tmp_path), key)
    assert out.mode == "RGB"
    assert out.size == (784, 1568)  # portrait after transpose, long side capped


def test_load_rgba_png_flattens_on_white(tmp_path):
    im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    key = _write(tmp_path, "a.png", im, "PNG")
    out = images.load_normalized(LocalStore(tmp_path), key)
    assert out.mode == "RGB"
    assert out.getpixel((5, 5)) == (255, 255, 255)


def test_load_heic(tmp_path):
    pytest.importorskip("pillow_heif")
    assert images.HEIC_AVAILABLE
    key = _write(tmp_path, "a.heic", Image.new("RGB", (64, 48), "blue"), "HEIF")
    assert images.load_normalized(LocalStore(tmp_path), key).size == (64, 48)


@pytest.mark.parametrize("name", ["a.heic", "a.heif"])
def test_load_heic_without_extra_says_to_export_jpeg(tmp_path, monkeypatch, name):
    monkeypatch.setattr(images, "HEIC_AVAILABLE", False)
    key = f"images/1/original/{name}"
    LocalStore(tmp_path).put(key, b"not decoded")
    with pytest.raises(HeicExtraMissingError, match="not supported"):
        images.load_normalized(LocalStore(tmp_path), key)


def test_core_imports_without_pillow_heif():
    code = (
        "import sys; sys.modules['pillow_heif'] = None; "
        "import kyapehnu.agent.llm, kyapehnu.wardrobe.ingest.tagging, kyapehnu.wardrobe.ingest.dedupe; "
        "import kyapehnu.wardrobe.ingest.images as i; sys.exit(i.HEIC_AVAILABLE)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_load_corrupt_raises(tmp_path):
    key = "images/1/original/bad.jpg"
    LocalStore(tmp_path).put(key, b"not an image")
    with pytest.raises(UnidentifiedImageError):
        images.load_normalized(LocalStore(tmp_path), key)


def test_load_rejects_over_pixel_limit(tmp_path, monkeypatch):
    key = _write(tmp_path, "a.png", Image.new("RGB", (20, 20)), "PNG")
    monkeypatch.setattr(images, "MAX_PIXELS", 100)
    with pytest.raises(ImageTooLargeError, match="20x20"):
        images.load_normalized(LocalStore(tmp_path), key)


def test_load_accepts_exactly_pixel_limit(tmp_path, monkeypatch):
    key = _write(tmp_path, "a.png", Image.new("RGB", (20, 20)), "PNG")
    monkeypatch.setattr(images, "MAX_PIXELS", 400)
    assert images.load_normalized(LocalStore(tmp_path), key).size == (20, 20)


def test_normalize_bbox_passes_through_fractions():
    assert images.normalize_bbox([0.1, 0.2, 0.5, 0.6], 1000, 500) == [0.1, 0.2, 0.5, 0.6]


def test_normalize_bbox_converts_pixels():
    assert images.normalize_bbox([100, 50, 500, 250], 1000, 500) == [0.1, 0.1, 0.5, 0.5]


def test_normalize_bbox_sorts_and_clamps():
    assert images.normalize_bbox([0.9, 1.3, -0.2, 0.4], 100, 100) == [0.0, 0.4, 0.9, 1.0]


def test_pad_and_area():
    b = images.pad_bbox([0.2, 0.2, 0.6, 0.6], 0.05)
    assert b == pytest.approx([0.18, 0.18, 0.62, 0.62])
    assert images.bbox_area([0.0, 0.0, 0.5, 0.5]) == 0.25
    assert images.pad_bbox([0.0, 0.0, 1.0, 1.0], 0.05) == [0.0, 0.0, 1.0, 1.0]


def test_union_bbox():
    assert images.union_bbox([[0.1, 0.2, 0.3, 0.4], [0.2, 0.1, 0.5, 0.3]]) == [0.1, 0.1, 0.5, 0.4]


def test_crop_never_empty():
    im = Image.new("RGB", (100, 100))
    assert images.crop(im, [0.5, 0.5, 0.5, 0.5]).size == (1, 1)


@pytest.mark.parametrize(
    "box",
    [[1.0, 1.0, 1.0, 1.0], [1.0, 0.0, 1.0, 1.0], [0.0, 1.0, 1.0, 1.0], [0.99, 0.99, 1.0, 1.0]],
    ids=["corner", "right_edge", "bottom_edge", "tiny_in_corner"],
)
def test_crop_of_a_box_on_the_right_or_bottom_edge_is_never_empty(box):
    im = Image.new("RGB", (100, 50))

    width, height = images.crop(im, box).size

    assert width >= 1 and height >= 1


def test_crop_selects_region():
    im = Image.new("RGB", (100, 100), "white")
    im.paste(Image.new("RGB", (50, 50), "red"), (0, 0))
    assert images.crop(im, [0.0, 0.0, 0.5, 0.5]).getpixel((10, 10)) == (255, 0, 0)


def test_make_cutout_falls_back_when_rembg_fails(monkeypatch):
    def boom(im):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(images, "remove_background", boom)
    im = Image.new("RGB", (10, 10))
    assert images.make_cutout(im, use_rembg=True) is im


def test_make_cutout_skips_rembg_for_worn(monkeypatch):
    called = []
    monkeypatch.setattr(images, "remove_background", lambda im: called.append(1) or im)
    images.make_cutout(Image.new("RGB", (10, 10)), use_rembg=False)
    assert called == []


def test_make_cutout_uses_rembg_when_asked():
    out = images.make_cutout(Image.new("RGB", (10, 10)), use_rembg=True)
    assert out.mode == "RGBA"  # the autouse fake converts to RGBA


def test_save_cutout_writes_png_and_webp_thumb(tmp_path):
    store = LocalStore(tmp_path)
    cut, thumb = images.save_cutout(Image.new("RGBA", (1200, 800), "red"), store, 1, "abc")
    assert cut == "images/1/cutout/abc.png" and thumb == "images/1/thumb/abc.webp"
    assert Image.open(tmp_path / thumb).size == (400, 267)
    assert Image.open(tmp_path / cut).size == (1200, 800)


def test_to_jpeg_bytes_flattens_rgba():
    out = Image.open(io.BytesIO(images.to_jpeg_bytes(Image.new("RGBA", (8, 8), (0, 0, 0, 0)))))
    assert out.format == "JPEG" and out.getpixel((1, 1)) == (255, 255, 255)


def test_phash_distance():
    a = Image.open(io.BytesIO(noise_image_bytes(1)))
    b = Image.open(io.BytesIO(noise_image_bytes(2)))
    assert images.hamming(images.phash(a), images.phash(a)) == 0
    assert images.hamming(images.phash(a), images.phash(b)) > 10


def test_failed_background_removal_logs_one_line_without_a_traceback(monkeypatch, caplog):
    import logging

    def no_network(im):
        raise ConnectionError("Failed to resolve 'github.com'")

    monkeypatch.setattr(images, "remove_background", no_network)

    with caplog.at_level(logging.DEBUG):
        images.make_cutout(Image.new("RGB", (10, 10)), use_rembg=True)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and warnings[0].exc_info is None
    assert "event=rembg_failed" in warnings[0].getMessage() and "ConnectionError" in warnings[0].getMessage()
    assert "github.com" not in warnings[0].getMessage()
