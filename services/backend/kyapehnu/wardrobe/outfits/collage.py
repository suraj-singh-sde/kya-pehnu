"""Draws the picture of an outfit: the items laid out on one 800x800 WebP. Never fails a wear."""

import io
import logging
from collections.abc import Callable, Sequence
from typing import Final
from uuid import uuid4

from PIL import Image, ImageOps

from kyapehnu.files import public as files
from kyapehnu.wardrobe.ingest import images
from kyapehnu.wardrobe.outfits.slots import EXTRA_SLOTS, Slot, SlottedItem

log = logging.getLogger(__name__)

SIZE: Final = 800
HALF: Final = SIZE // 2
ACCESSORY_HEIGHT: Final = 200
PADDING: Final = 16
BACKGROUND: Final = (245, 245, 244)
LEFT_COLUMN: Final = (Slot.outerwear, Slot.top, Slot.blouse, Slot.full)
RIGHT_COLUMN: Final = (Slot.bottom, Slot.footwear)

Box = tuple[int, int, int, int]  # left, top, right, bottom


def _load(store: files.FileStore, item_path: str | None) -> Image.Image | None:
    """The picture as RGBA, or None when it is missing or unreadable (that item is left out). A store that does not
    answer is not a missing picture: it escapes, so the caller can answer "try again" instead of waiting per item."""
    if not item_path:
        return None
    try:
        with store.open(item_path) as f, Image.open(f) as im:
            return im.convert("RGBA")
    except files.StoreUnavailableError:
        raise
    except Exception as e:  # a missing object, or corrupt data (OSError, SyntaxError, ValueError, ...)
        log.warning("event=collage_item_skipped error=%s", type(e).__name__)
        return None


def _stack(pictures: list[Image.Image], box: Box) -> list[tuple[Image.Image, Box]]:
    """Equal-height cells, one above the other."""
    left, top, right, bottom = box
    height = (bottom - top) // len(pictures) if pictures else 0
    return [(p, (left, top + n * height, right, top + (n + 1) * height)) for n, p in enumerate(pictures)]


def _row(pictures: list[Image.Image], box: Box) -> list[tuple[Image.Image, Box]]:
    """Equal-width cells side by side."""
    left, top, right, bottom = box
    width = (right - left) // len(pictures) if pictures else 0
    return [(p, (left + n * width, top, left + (n + 1) * width, bottom)) for n, p in enumerate(pictures)]


def _paste(canvas: Image.Image, picture: Image.Image, cell: Box) -> None:
    left, top, right, bottom = cell
    fitted = ImageOps.contain(picture, (right - left - 2 * PADDING, bottom - top - 2 * PADDING))
    x = left + (right - left - fitted.width) // 2
    y = top + (bottom - top - fitted.height) // 2
    canvas.paste(fitted, (x, y), fitted)  # the picture's own alpha is the mask, so cut-outs show the background


Loader = Callable[[files.FileStore, str | None], Image.Image | None]


def memoised_loader() -> Loader:
    """`_load` that reads each path once: the candidates of one suggest share most of their items."""
    seen: dict[str | None, Image.Image | None] = {}

    def load(store: files.FileStore, item_path: str | None) -> Image.Image | None:
        if item_path not in seen:
            seen[item_path] = _load(store, item_path)
        return seen[item_path]

    return load


def jpeg_bytes(picture: Image.Image) -> bytes:
    """The picture as a JPEG for a model to look at: a tenth of the PNG, and every provider reads it."""
    buf = io.BytesIO()
    picture.save(buf, "JPEG", quality=images.JPEG_QUALITY)
    return buf.getvalue()


def png_bytes(picture: Image.Image) -> bytes:
    buf = io.BytesIO()
    picture.save(buf, "PNG")
    return buf.getvalue()


def draw(store: files.FileStore, items: Sequence[SlottedItem], load: Loader = _load) -> Image.Image | None:
    """The outfit laid out on one canvas, or None when no item has a readable picture."""
    pictures: dict[Slot, list[Image.Image]] = {slot: [] for slot in Slot}
    for slot, item in items:
        if (picture := load(store, item.thumb_path or item.cutout_path)) is not None:
            pictures[slot].append(picture)
    left = [p for slot in LEFT_COLUMN for p in pictures[slot]]
    right = [p for slot in RIGHT_COLUMN for p in pictures[slot]]
    accessories = [p for slot in (Slot.accessory, *EXTRA_SLOTS) for p in pictures[slot]]
    if not (left or right or accessories):
        return None
    shelf = SIZE - ACCESSORY_HEIGHT if accessories else SIZE
    cells = [
        *_stack(left, (0, 0, HALF, SIZE)),
        *_stack(right, (HALF, 0, SIZE, shelf)),
        *_row(accessories, (HALF, shelf, SIZE, SIZE)),
    ]
    canvas = Image.new("RGB", (SIZE, SIZE), BACKGROUND)
    for picture, cell in cells:
        _paste(canvas, picture, cell)
    return canvas


def render(store: files.FileStore, user_id: int, items: Sequence[SlottedItem]) -> str | None:
    """Draw the items, store the picture and return its key, or None when nothing could be drawn or stored."""
    try:
        canvas = draw(store, items)
        if canvas is None:
            return None
        encoded = io.BytesIO()
        canvas.save(encoded, "WEBP", quality=images.WEBP_QUALITY)
        key = files.key(user_id, "collage", f"{uuid4().hex}.webp")
        store.put(key, encoded.getvalue())
    except Exception as e:  # a wear is never failed by its picture; put is all or nothing, so nothing is left behind
        log.warning("event=collage_failed error=%s", type(e).__name__)
        return None
    return key
