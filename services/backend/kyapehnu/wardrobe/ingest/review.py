from collections.abc import Sequence
from typing import Any

from sqlmodel import Session, col, select

from kyapehnu.core.context import Ctx
from kyapehnu.core.db import get_owned
from kyapehnu.core.errors import ReviewError as ReviewError
from kyapehnu.files import public as files
from kyapehnu.wardrobe import items
from kyapehnu.wardrobe.ingest import images
from kyapehnu.wardrobe.ingest.pipeline import cut_piece
from kyapehnu.wardrobe.models import IngestPiece, Item, Photo, PhotoType, PieceState

REVIEWABLE = (PieceState.pending_review, PieceState.error, PieceState.rejected)


def queue(user_id: int, s: Session, states: Sequence[str] = REVIEWABLE, job_id: int | None = None) -> list[IngestPiece]:
    stmt = (
        select(IngestPiece)
        .where(IngestPiece.user_id == user_id, col(IngestPiece.state).in_(states))
        .order_by(col(IngestPiece.id))
    )
    if job_id is not None:
        stmt = stmt.where(IngestPiece.job_id == job_id)
    return list(s.exec(stmt).all())


def _require_state(piece: IngestPiece, *allowed: str) -> None:
    if piece.state not in allowed:
        raise ReviewError(f"piece {piece.id} is already {piece.state}")


def require_pending(piece: IngestPiece) -> IngestPiece:
    """Only a piece still in the review queue may be changed."""
    _require_state(piece, PieceState.pending_review)
    return piece


def accept_piece(s: Session, piece: IngestPiece, overrides: dict[str, Any] | None = None) -> Item:
    _require_state(piece, PieceState.pending_review)
    tags = piece.proposed_tags or {}
    item = Item(
        user_id=piece.user_id,
        photo_id=piece.photo_id,
        bbox=piece.bbox,
        cutout_path=piece.cutout_path,
        thumb_path=piece.thumb_path,
        alt_cutout_path=piece.alt_cutout_path,
        alt_thumb_path=piece.alt_thumb_path,
        picture_generated=piece.picture_generated,
        phash=piece.phash,
        ai_tags=tags,
        **tags,
    )
    s.add(item)
    s.flush()
    if overrides:
        items.update_item(s, item, overrides)
    piece.state, piece.item_id = PieceState.accepted, item.id
    s.commit()
    s.refresh(item)
    return item


def discard_piece(s: Session, piece: IngestPiece) -> IngestPiece:
    _require_state(piece, *REVIEWABLE)
    piece.state = PieceState.discarded
    s.commit()
    s.refresh(piece)
    return piece


def mark_duplicate(s: Session, piece: IngestPiece, item_id: int) -> IngestPiece:
    _require_state(piece, PieceState.pending_review)
    get_owned(piece.user_id, s, Item, item_id)
    piece.state, piece.duplicate_of_item_id = PieceState.discarded, item_id
    s.commit()
    s.refresh(piece)
    return piece


def merge_pieces(ctx: Ctx, s: Session, piece: IngestPiece, other_ids: list[int]) -> IngestPiece:
    """Merge other pieces of the same photo into this one: union box, new cutout, ethnic wins the category."""
    ids = dict.fromkeys(i for i in other_ids if i != piece.id)
    if not ids:
        raise ReviewError("pick at least one other piece to merge")
    others = [get_owned(piece.user_id, s, IngestPiece, i) for i in ids]
    group = [piece, *others]
    for p in group:
        _require_state(p, PieceState.pending_review)
        if p.photo_id != piece.photo_id:
            raise ReviewError("can only merge pieces from the same photo")

    photo = get_owned(piece.user_id, s, Photo, piece.photo_id)
    box = images.union_bbox([p.bbox for p in group])
    img = images.load_normalized(ctx.files, str(photo.normalized_path))
    old_files = [
        key for p in group for key in (p.cutout_path, p.thumb_path, p.alt_cutout_path, p.alt_thumb_path) if key
    ]
    piece.cutout_path, piece.thumb_path, piece.phash = cut_piece(
        ctx, piece.user_id, img, box, use_rembg=photo.photo_type != PhotoType.worn
    )
    new_files = [piece.cutout_path, piece.thumb_path]
    piece.alt_cutout_path, piece.alt_thumb_path, piece.picture_generated = None, None, False  # of the old crop
    piece.bbox = box
    if any((p.proposed_tags or {}).get("category") == "ethnic" for p in group):
        piece.proposed_tags = {**(piece.proposed_tags or {}), "category": "ethnic"}
    for o in others:
        o.state, o.cutout_path, o.thumb_path = PieceState.merged, None, None  # their files are removed below
        o.alt_cutout_path, o.alt_thumb_path, o.picture_generated = None, None, False
    try:
        s.commit()
    except Exception:
        s.rollback()
        files.discard(ctx.files, new_files)
        raise
    files.discard(ctx.files, old_files)  # the rows are committed; the orphan sweep removes what is left
    s.refresh(piece)
    return piece
