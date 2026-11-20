"""Helpers for the wear, collage and outfit API tests: picture files, items with pictures, event rows, counters."""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import func
from sqlmodel import Session, SQLModel, select

from kyapehnu.wardrobe.models import Event, Item
from tests.fakes import OWNER_ID, add_item, ensure_owner, stored_files

TODAY = date(2026, 10, 6)

RED = (220, 30, 30)
GREEN = (30, 160, 30)
BLUE = (30, 30, 220)
YELLOW = (230, 200, 20)
PURPLE = (140, 30, 160)
BACKGROUND = (245, 245, 244)


def write_picture(data_dir: Path, rel: str, rgb: tuple[int, int, int] = RED, size: tuple[int, int] = (200, 200)) -> str:
    """A solid-colour picture. The format follows the extension (.webp lossless, .png), like the stored pictures."""
    path = data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    picture = Image.new("RGB", size, rgb)
    if path.suffix == ".webp":
        picture.save(path, lossless=True)
    else:
        picture.save(path)
    return rel


def pictured(s: Session, data_dir: Path, name: str, rgb: tuple[int, int, int] = RED, **fields: Any) -> Item:
    """A committed item whose thumbnail is a solid picture on disk."""
    rel = write_picture(data_dir, f"images/{OWNER_ID}/thumb/{name.replace(' ', '_').lower()}.webp", rgb)
    return add_item(s, name=name, thumb_path=rel, **fields)


def three_items(s: Session, data_dir: Path) -> tuple[Item, Item, Item]:
    """A top, a bottom and footwear, in that order, so their ids ascend in slot order."""
    top = pictured(s, data_dir, "White shirt", RED, category="top")
    bottom = pictured(s, data_dir, "Navy chinos", BLUE, category="bottom")
    shoes = pictured(s, data_dir, "Brown loafers", GREEN, category="footwear")
    return top, bottom, shoes


def add_event(s: Session, **fields: Any) -> Event:
    """A committed party tomorrow evening (relative to TODAY unless starts_at is given)."""
    event = Event(
        **{
            "user_id": ensure_owner(s),
            "type": "party",
            "title": "Dinner",
            "starts_at": datetime.combine(TODAY + timedelta(days=1), datetime.min.time()).replace(hour=19),
            "place": "Gurugram",
            "audience_tags": ["Priya"],
            **fields,
        }
    )
    s.add(event)
    s.commit()
    s.refresh(event)
    return event


def count(s: Session, model: type[SQLModel]) -> int:
    return s.exec(select(func.count()).select_from(model)).one()


def stored(data_dir: Path, kind: str = "collage") -> list[str]:
    """Names of the owner's files under data/images/<user_id>/<kind> (empty when the folder is missing)."""
    return stored_files(data_dir, kind)
