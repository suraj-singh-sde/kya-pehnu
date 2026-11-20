"""Image helpers for ingestion. Pictures are read and written through the files store; keys are what rows name."""

import io
import logging
from collections.abc import Sequence
from functools import cache
from pathlib import PurePosixPath
from typing import Any, Final

import imagehash
from PIL import Image, ImageOps

from kyapehnu.core.errors import HeicExtraMissingError, ImageTooLargeError
from kyapehnu.files import public as files

# HEIC support is the optional `heic` extra (LGPL/GPL codecs): the core must import without it.
HEIC_AVAILABLE: bool
try:
    from pillow_heif import register_heif_opener
except ImportError:
    HEIC_AVAILABLE = False
else:
    register_heif_opener()
    HEIC_AVAILABLE = True

log = logging.getLogger(__name__)

HEIC_EXTS: Final = frozenset({".heic", ".heif"})
IMAGE_EXTS: Final = frozenset({".jpg", ".jpeg", ".png", ".webp"}) | HEIC_EXTS
MAX_SIDE: Final = 1568
THUMB_SIDE: Final = 400
JPEG_QUALITY: Final = 90
WEBP_QUALITY: Final = 85
MAX_PIXELS: Final = 89_478_485  # Pillow's default MAX_IMAGE_PIXELS, enforced here at 1x instead of warning
PIXEL_COORD_THRESHOLD: Final = 2  # bbox values above this are pixel coordinates, not fractions
REMBG_MODEL: Final = "u2net"
TRIM_MARGIN: Final = 0.04  # room left around the garment in a thumb, as a share of its longer side
SOLID_ALPHA: Final = 8  # below this a pixel counts as transparent when finding the garment's edges


def flatten(im: Image.Image) -> Image.Image:
    """RGB copy; transparent areas become white."""
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


def load_normalized(store: files.FileStore, key: str) -> Image.Image:
    """Upright RGB image with the long side capped at MAX_SIDE."""
    if PurePosixPath(key).suffix.lower() in HEIC_EXTS and not HEIC_AVAILABLE:
        raise HeicExtraMissingError()
    with store.open(key) as f, Image.open(f) as src:
        if src.width * src.height > MAX_PIXELS:
            raise ImageTooLargeError(f"image too large: {src.width}x{src.height} pixels (limit {MAX_PIXELS})")
        im = flatten(ImageOps.exif_transpose(src))
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    return im


def to_jpeg_bytes(im: Image.Image, quality: int = JPEG_QUALITY) -> bytes:
    buf = io.BytesIO()
    flatten(im).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _clamp(v: float) -> float:
    return min(1.0, max(0.0, float(v)))


def normalize_bbox(bbox: Sequence[float], width: int, height: int) -> list[float]:
    x0, y0, x1, y1 = bbox
    # ponytail: values > 2 are treated as pixel coords (Qwen-VL); 0-1000 grids (Gemini) are not detected
    if max(bbox) > PIXEL_COORD_THRESHOLD:
        x0, x1, y0, y1 = x0 / width, x1 / width, y0 / height, y1 / height
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    return [_clamp(x0), _clamp(y0), _clamp(x1), _clamp(y1)]


def pad_bbox(b: Sequence[float], pad: float) -> list[float]:
    w, h = b[2] - b[0], b[3] - b[1]
    return [_clamp(b[0] - w * pad), _clamp(b[1] - h * pad), _clamp(b[2] + w * pad), _clamp(b[3] + h * pad)]


def bbox_area(b: Sequence[float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def union_bbox(boxes: Sequence[Sequence[float]]) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def crop(im: Image.Image, b: Sequence[float]) -> Image.Image:
    w, h = im.size
    x0, y0 = min(int(b[0] * w), w - 1), min(int(b[1] * h), h - 1)
    x1, y1 = max(x0 + 1, round(b[2] * w)), max(y0 + 1, round(b[3] * h))
    return im.crop((x0, y0, min(x1, w), min(y1, h)))


@cache
def _rembg_session() -> Any:
    from rembg import new_session

    return new_session(REMBG_MODEL)


def remove_background(im: Image.Image) -> Image.Image:
    from rembg import remove

    cutout: Image.Image = remove(im, session=_rembg_session())
    return cutout


def make_cutout(im: Image.Image, use_rembg: bool) -> Image.Image:
    if not use_rembg:
        return im
    try:
        return remove_background(im)
    except Exception as e:  # the model may be missing or fail to download: a plain crop is still useful
        log.warning("event=rembg_failed error=%s; keeping the plain crop", type(e).__name__)
        log.debug("event=rembg_failed_trace", exc_info=True)
        return im


def _encode(im: Image.Image, fmt: str, **options: Any) -> bytes:
    buf = io.BytesIO()
    im.save(buf, fmt, **options)
    return buf.getvalue()


def trim(im: Image.Image) -> Image.Image:
    """Crop to the garment with a small margin, so every thumb shows its garment at the same scale whatever
    transparent border the cutout came with. A picture without transparency is returned as it is."""
    if im.mode != "RGBA":
        return im
    box = im.getchannel("A").point(lambda a: 255 if a >= SOLID_ALPHA else 0).getbbox()
    if not box:
        return im
    pad = round(max(box[2] - box[0], box[3] - box[1]) * TRIM_MARGIN)
    x0, y0, x1, y1 = box
    return im.crop((max(0, x0 - pad), max(0, y0 - pad), min(im.width, x1 + pad), min(im.height, y1 + pad)))


def thumb_bytes(im: Image.Image) -> bytes:
    thumb = trim(im).copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE))
    return _encode(thumb, "WEBP", quality=WEBP_QUALITY)


def save_cutout(im: Image.Image, store: files.FileStore, user_id: int, stem: str) -> tuple[str, str]:
    """Store the cutout PNG and its thumb WEBP. Returns their keys. Both are stored or neither is."""
    cut_key = files.key(user_id, "cutout", f"{stem}.png")
    thumb_key = files.key(user_id, "thumb", f"{stem}.webp")
    try:
        store.put(cut_key, _encode(im, "PNG"))
        store.put(thumb_key, thumb_bytes(im))
    except Exception:
        files.discard(store, [cut_key, thumb_key])
        raise
    return cut_key, thumb_key


def phash(im: Image.Image) -> str:
    return str(imagehash.phash(flatten(im)))


def hamming(a: str, b: str) -> int:
    return int(imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b))
