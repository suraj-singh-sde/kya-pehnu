"""Helpers of the WP-3 ownership tests: rows and image files that belong to an explicit user.

Every row is built with an explicit `user_id`, so no test here depends on the legacy-owner stamper
(tests/_legacy_owner_wardrobe.py)."""

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from PIL import Image
from sqlmodel import Session, SQLModel, select

from kyapehnu.wardrobe.models import (
    Event,
    Feedback,
    IngestJob,
    IngestPiece,
    Item,
    ItemCorrection,
    JobStatus,
    Outfit,
    Photo,
    PhotoStatus,
    WearLog,
)

OWNED_MODELS: tuple[type[SQLModel], ...] = (
    IngestJob,
    Photo,
    Item,
    ItemCorrection,
    IngestPiece,
    Event,
    Outfit,
    WearLog,
    Feedback,
)
KINDS = ("original", "cutout", "thumb", "collage", "mannequin")
WORN_ON = date(2026, 10, 1)


def me(client) -> int:
    """The user id of the account the client is signed in as."""
    return int(client.get("/api/auth/me").json()["id"])


def write_image(data_dir: Path, rel: str, rgb: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    """A small picture at `data_dir/rel` (format by extension). Returns the bytes written."""
    path = data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = {".png": "PNG", ".webp": "WEBP", ".jpg": "JPEG", ".jpeg": "JPEG"}[path.suffix]
    Image.new("RGB", (32, 32), rgb).save(path, fmt)
    return path.read_bytes()


def image_files(data_dir: Path) -> list[str]:
    """Every file under data_dir/images, relative and sorted."""
    root = data_dir / "images"
    return sorted(p.relative_to(data_dir).as_posix() for p in root.rglob("*") if p.is_file()) if root.is_dir() else []


def add_item(ctx, user_id: int, name: str = "Item", **fields: Any) -> Item:
    with Session(ctx.engine) as s:
        item = Item(user_id=user_id, name=name, **{"category": "top", **fields})
        s.add(item)
        s.commit()
        s.refresh(item)
        return item


@dataclass
class World:
    """One user's complete data: a row of every owned table plus the image files they point at."""

    user_id: int
    job: int
    photo: int
    top: int
    bottom: int
    correction: int
    piece: int
    event: int
    outfit: int
    wear: int
    feedback: int
    files: dict[str, bytes]  # rel path -> bytes on disk

    @property
    def items(self) -> list[int]:
        return [self.top, self.bottom]


def seed_world(ctx, user_id: int, tag: str) -> World:
    """Rows of every owned table for `user_id`, with their files under images/<user_id>/<kind>/."""
    dd = ctx.data_dir
    rgb = (200, 30, 30) if tag == "a" else (30, 30, 200)
    rels = {
        "original": f"images/{user_id}/original/{tag}orig.jpg",
        "cutout_top": f"images/{user_id}/cutout/{tag}top.png",
        "thumb_top": f"images/{user_id}/thumb/{tag}top.webp",
        "cutout_bottom": f"images/{user_id}/cutout/{tag}bottom.png",
        "thumb_bottom": f"images/{user_id}/thumb/{tag}bottom.webp",
        "cutout_piece": f"images/{user_id}/cutout/{tag}piece.png",
        "thumb_piece": f"images/{user_id}/thumb/{tag}piece.webp",
        "collage": f"images/{user_id}/collage/{tag}collage.webp",
    }
    files = {rel: write_image(dd, rel, rgb) for rel in rels.values()}
    with Session(ctx.engine) as s:
        job = IngestJob(user_id=user_id, source="upload", total=1, processed=1, status=JobStatus.done)
        s.add(job)
        s.flush()
        photo = Photo(
            user_id=user_id, job_id=job.id, original_path=rels["original"], status=PhotoStatus.failed, error="x"
        )
        s.add(photo)
        s.flush()
        top = Item(
            user_id=user_id,
            photo_id=photo.id,
            name=f"{tag} shirt",
            category="top",
            cutout_path=rels["cutout_top"],
            thumb_path=rels["thumb_top"],
        )
        bottom = Item(
            user_id=user_id,
            name=f"{tag} chinos",
            category="bottom",
            cutout_path=rels["cutout_bottom"],
            thumb_path=rels["thumb_bottom"],
        )
        s.add_all([top, bottom])
        s.flush()
        correction = ItemCorrection(user_id=user_id, item_id=top.id, field="name", ai_value="x", user_value="y")
        piece = IngestPiece(
            user_id=user_id,
            job_id=job.id,
            photo_id=photo.id,
            bbox=[0, 0, 1, 1],
            cutout_path=rels["cutout_piece"],
            thumb_path=rels["thumb_piece"],
            proposed_tags={"name": f"{tag} piece", "category": "top"},
        )
        event = Event(
            user_id=user_id, type="party", title=f"{tag} dinner", starts_at=datetime(2026, 10, 20, 19), place="Pune"
        )
        s.add_all([correction, piece, event])
        s.flush()
        ids = sorted([top.id, bottom.id])
        outfit = Outfit(
            user_id=user_id,
            item_ids=ids,
            item_key=",".join(map(str, ids)),
            name=f"{tag} outfit",
            collage_path=rels["collage"],
        )
        s.add(outfit)
        s.flush()
        wear = WearLog(user_id=user_id, outfit_id=outfit.id, item_ids=ids, worn_on=WORN_ON, event_id=event.id)
        feedback = Feedback(user_id=user_id, outfit_id=outfit.id, rating=1, reason=f"{tag} liked it")
        s.add_all([wear, feedback])
        s.commit()
        return World(
            user_id=user_id,
            job=job.id,
            photo=photo.id,
            top=top.id,
            bottom=bottom.id,
            correction=correction.id,
            piece=piece.id,
            event=event.id,
            outfit=outfit.id,
            wear=wear.id,
            feedback=feedback.id,
            files=files,
        )


def snapshot(ctx) -> str:
    """Every row of every owned table (all users) and every image file with its bytes, as one comparable string."""
    out: dict[str, Any] = {}
    with Session(ctx.engine) as s:
        for model in OWNED_MODELS:
            rows = s.exec(select(model).order_by(model.id)).all()  # type: ignore[attr-defined]
            out[model.__name__] = [r.model_dump(mode="json") for r in rows]
    out["files"] = {rel: (ctx.data_dir / rel).read_bytes().hex() for rel in image_files(ctx.data_dir)}
    return json.dumps(out, sort_keys=True)


def rows_of(ctx, model: type[SQLModel], user_id: int) -> list[Any]:
    with Session(ctx.engine) as s:
        return list(s.exec(select(model).where(model.user_id == user_id).order_by(model.id)).all())  # type: ignore[attr-defined]
