from datetime import date
from typing import Annotated, Any, Final, cast

from pydantic import BaseModel, Field
from sqlmodel import Session, col, delete, func, or_, select

from kyapehnu.core.db import get_owned, utcnow
from kyapehnu.core.schemas import TAG_FIELDS
from kyapehnu.files import public as files
from kyapehnu.wardrobe.models import Category, IngestPiece, Item, ItemCorrection, ItemStatus, Photo, PieceState, Season
from kyapehnu.wardrobe.outfits import mannequin, wear
from kyapehnu.wardrobe.outfits.repetition import is_forgotten
from kyapehnu.wardrobe.owned import find_items, require_owned_items

MAX_QUERY_CHARS: Final = 120

# PATCH: an explicit null clears these; for every other field it means "no change".
NULLABLE_FIELDS = frozenset({"fabric", "fit", "brand", "purchase_price", "purchase_date"})


class WardrobeQuery(BaseModel):
    """The filters of GET /api/items, also the arguments of the chat's search tool. Every filter is optional."""

    category: Category | None = None
    color: Annotated[str | None, Field(max_length=MAX_QUERY_CHARS)] = None
    formality_min: Annotated[int | None, Field(ge=1, le=5)] = None
    formality_max: Annotated[int | None, Field(ge=1, le=5)] = None
    season: Season | None = None
    status: ItemStatus | None = None
    q: Annotated[str | None, Field(max_length=MAX_QUERY_CHARS)] = None
    not_worn_days: Annotated[int | None, Field(ge=1, le=3650)] = None


def list_items(
    user_id: int,
    s: Session,
    *,
    category: str | None = None,
    color: str | None = None,
    formality_min: int | None = None,
    formality_max: int | None = None,
    season: str | None = None,
    status: str | None = None,
    q: str | None = None,
    not_worn_days: int | None = None,
    brand: bool = True,
    today: date | None = None,
) -> list[Item]:
    stmt = select(Item).where(Item.user_id == user_id).order_by(col(Item.created_at).desc(), col(Item.id).desc())
    stmt = stmt.where(Item.status == status) if status else stmt.where(Item.status != ItemStatus.retired)
    if category:
        stmt = stmt.where(Item.category == category)
    if formality_min is not None:
        stmt = stmt.where(Item.formality >= formality_min)
    if formality_max is not None:
        stmt = stmt.where(Item.formality <= formality_max)
    result = list(s.exec(stmt).all())
    # ponytail: JSON-list filters run in Python; fine for a personal wardrobe (< ~5k items)
    if color:
        result = [i for i in result if any(c.get("name", "").lower() == color.lower() for c in i.colors)]
    if season:
        result = [i for i in result if season in i.seasons]
    if q:
        needle = q.lower()
        result = [
            i
            for i in result
            if needle in i.name.lower()
            or (brand and needle in (i.brand or "").lower())
            or any(needle in t.lower() for t in i.tags)
        ]
    if not_worn_days is not None:  # never worn, or last worn that many days ago or longer; "worn today" counts
        last_worn, now = wear.last_worn_by_item(user_id, s), today or date.today()
        result = [i for i in result if is_forgotten(last_worn.get(cast(int, i.id)), now, not_worn_days)]
    return result


def get_detail(user_id: int, s: Session, item_id: int) -> tuple[Item, list[Item], list[ItemCorrection], str | None]:
    """The item, the other items cut from the same photo, its corrections and the photo's normalized path."""
    item = get_owned(user_id, s, Item, item_id)
    photo = s.get(Photo, item.photo_id) if item.photo_id is not None else None
    siblings = (
        list(
            s.exec(
                select(Item)
                .where(Item.user_id == user_id, Item.photo_id == photo.id, Item.id != item.id)
                .order_by(col(Item.id))
            ).all()
        )
        if photo
        else []
    )
    corrections = list(
        s.exec(
            select(ItemCorrection)
            .where(ItemCorrection.user_id == user_id, ItemCorrection.item_id == item_id)
            .order_by(col(ItemCorrection.id))
        ).all()
    )
    return item, siblings, corrections, photo.normalized_path if photo else None


def commit_patch(user_id: int, s: Session, item_id: int, sent: dict[str, Any]) -> Item:
    """Apply the fields a client sent (null means "no change" except for NULLABLE_FIELDS) and commit. Nothing is read
    back, so an error raised here means the change was not committed."""
    changes = {field: value for field, value in sent.items() if value is not None or field in NULLABLE_FIELDS}
    item = update_item(s, get_owned(user_id, s, Item, item_id), changes)
    s.commit()
    return item


def patch_item(user_id: int, s: Session, item_id: int, sent: dict[str, Any]) -> Item:
    item = commit_patch(user_id, s, item_id, sent)
    s.refresh(item)
    return item


def set_status(user_id: int, s: Session, item_id: int, status: ItemStatus) -> Item:
    return patch_item(user_id, s, item_id, {"status": status})


def set_status_many(user_id: int, s: Session, item_ids: list[int], status: ItemStatus) -> list[Item]:
    """Set one status on every item, or on none when any id is unknown."""
    found = require_owned_items(user_id, s, item_ids)
    for item in found.values():
        update_item(s, item, {"status": status})
    s.commit()
    updated = find_items(user_id, s, item_ids)
    return [updated[item_id] for item_id in item_ids]


def update_item(s: Session, item: Item, changes: dict[str, Any]) -> Item:
    """Apply changes; record an ItemCorrection when a tag field newly differs from the AI's proposal. No commit."""
    for field, new in changes.items():
        if new == getattr(item, field):
            continue
        setattr(item, field, new)
        if field in TAG_FIELDS and item.ai_tags is not None and new != item.ai_tags.get(field):
            s.add(
                ItemCorrection(
                    user_id=item.user_id,
                    item_id=cast(int, item.id),
                    field=field,
                    ai_value=item.ai_tags.get(field),
                    user_value=new,
                )
            )
    item.updated_at = utcnow()
    s.add(item)
    return item


def _detach_pieces(s: Session, item: Item) -> None:
    """Clear every piece reference to the item. An accepted piece shares the item's files, which are about to go."""
    pieces = s.exec(
        select(IngestPiece).where(
            IngestPiece.user_id == item.user_id,
            or_(IngestPiece.item_id == item.id, IngestPiece.duplicate_of_item_id == item.id),
        )
    ).all()
    for p in pieces:
        if p.item_id == item.id:
            p.item_id = None
            if p.cutout_path == item.cutout_path:
                p.cutout_path = None
            if p.thumb_path == item.thumb_path:
                p.thumb_path = None
            p.alt_cutout_path, p.alt_thumb_path, p.picture_generated = None, None, False
        if p.duplicate_of_item_id == item.id:
            p.duplicate_of_item_id = None  # dup_reason stays, so the warning text survives


def _delete_photo_if_orphaned(s: Session, user_id: int, photo_id: int | None) -> list[str]:
    """Delete the photo with its pieces when no item and no unfinished piece needs it. Returns its file keys."""
    if photo_id is None:
        return []
    has_item = s.exec(
        select(func.count()).select_from(Item).where(Item.user_id == user_id, Item.photo_id == photo_id)
    ).one()
    unfinished = s.exec(
        select(func.count())
        .select_from(IngestPiece)
        .where(
            IngestPiece.user_id == user_id,
            IngestPiece.photo_id == photo_id,
            col(IngestPiece.state).in_([PieceState.pending_review, PieceState.error, PieceState.rejected]),
        )
    ).one()
    photo = s.get(Photo, photo_id)
    if has_item or unfinished or photo is None:
        return []
    pieces = s.exec(select(IngestPiece).where(IngestPiece.user_id == user_id, IngestPiece.photo_id == photo_id)).all()
    keys = [photo.original_path, photo.normalized_path]
    for p in pieces:
        keys += [p.cutout_path, p.thumb_path, p.alt_cutout_path, p.alt_thumb_path]
    s.exec(delete(IngestPiece).where(col(IngestPiece.user_id) == user_id, col(IngestPiece.photo_id) == photo_id))
    s.flush()
    s.delete(photo)
    return [key for key in keys if key]


def hard_delete(s: Session, item: Item, store: files.FileStore) -> None:
    """Delete the item, its corrections and, when nothing else needs it, its photo. Files go after the commit."""
    user_id, item_id, photo_id = item.user_id, item.id, item.photo_id
    keys = [key for key in (item.cutout_path, item.thumb_path, item.alt_cutout_path, item.alt_thumb_path) if key]
    s.exec(delete(ItemCorrection).where(col(ItemCorrection.user_id) == user_id, col(ItemCorrection.item_id) == item_id))
    _detach_pieces(s, item)
    s.flush()  # the pieces must stop pointing at the item before the item row goes (foreign keys are enforced)
    keys += wear.detach_item(user_id, s, cast(int, item_id))  # outfits keep the item's id but lose their collage
    s.delete(item)
    s.flush()
    keys += _delete_photo_if_orphaned(s, user_id, photo_id)
    s.commit()
    mannequin.remove_for_item(store, user_id, cast(int, item_id))
    files.discard(store, keys)  # the row is gone; a file that stays is the orphan sweep's to remove
