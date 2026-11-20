"""Delete pictures no row refers to (the startup reconciler of the store against rows).

It runs on a store that holds every user's pictures, so it refuses a doubtful picture of the world and deletes nothing:
a user with no rows, more candidates than the cap, a store that does not answer (ADR-5).
"""

import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from kyapehnu.files import public as files
from kyapehnu.wardrobe.models import IngestPiece, Item, Outfit, Photo
from kyapehnu.wardrobe.outfits import mannequin

log = logging.getLogger(__name__)

# Protects files a concurrent `kyapehnu seed` or an in-flight job just wrote but has not committed yet.
ORPHAN_MIN_AGE_SECONDS: Final = 3600
MANNEQUIN: Final = "mannequin"  # kept by saved outfit and age, not by reference
REFERENCING_COLUMNS: Final = (
    Photo.original_path,
    Photo.normalized_path,
    Item.cutout_path,
    Item.thumb_path,
    IngestPiece.cutout_path,
    IngestPiece.thumb_path,
    Item.alt_cutout_path,
    Item.alt_thumb_path,
    IngestPiece.alt_cutout_path,
    IngestPiece.alt_thumb_path,
    Outfit.collage_path,
)


def _referenced_paths(engine: Engine) -> set[str]:
    with Session(engine) as s:
        return {
            rel
            for column in REFERENCING_COLUMNS
            for rel in s.exec(select(column).where(col(column).is_not(None)))
            if rel is not None
        }


def _saved_outfit_keys(engine: Engine) -> dict[int, set[str]]:
    """The mannequin keys of every user's saved outfits."""
    keys: defaultdict[int, set[str]] = defaultdict(set)
    with Session(engine) as s:
        for user_id, ids in s.exec(select(Outfit.user_id, Outfit.item_ids)):
            keys[user_id].add(mannequin.key(ids))
    return keys


def _owners(engine: Engine) -> set[int]:
    """The users that have any row at all: a store folder of anyone else is not trusted to be an orphan."""
    with Session(engine) as s:
        return {
            user_id
            for column in (Photo.user_id, Item.user_id, IngestPiece.user_id, Outfit.user_id)
            for user_id in s.exec(select(column).distinct())
        }


def _candidates(stored: list[files.StoredFile], engine: Engine, now: float) -> list[str]:
    """The keys to delete: unreferenced pictures older than an hour, and unsaved mannequins older than 30 days."""
    referenced, saved, owners = _referenced_paths(engine), _saved_outfit_keys(engine), _owners(engine)
    pictures: defaultdict[int, list[files.StoredFile]] = defaultdict(list)
    mannequins: defaultdict[int, list[files.StoredFile]] = defaultdict(list)
    for found in stored:
        if (parsed := files.parse_key(found.key)) is not None:
            user_id, kind, _ = parsed
            (mannequins if kind == MANNEQUIN else pictures)[user_id].append(found)
    doomed: list[str] = []
    for user_id in pictures.keys() | mannequins.keys():
        if user_id not in owners:
            objects = len(pictures[user_id]) + len(mannequins[user_id])
            log.warning("event=orphan_sweep_skipped reason=no_rows_for_user user_id=%d objects=%d", user_id, objects)
            continue
        doomed += mannequin.stale(mannequins[user_id], saved[user_id], now)
        doomed += [
            f.key for f in pictures[user_id] if f.key not in referenced and now - f.modified > ORPHAN_MIN_AGE_SECONDS
        ]
    return doomed


def sweep_orphans(engine: Engine, store: files.FileStore, max_deletes: int, now: float | None = None) -> int:
    """Delete the pictures no row references (see `_candidates`); returns how many were deleted.

    A failed delete is logged by `files.discard` and left for the next start.
    """
    now = datetime.now(UTC).timestamp() if now is None else now
    try:
        # ponytail: the whole images/ prefix in memory; sweep one user prefix at a time if the store outgrows that
        stored = list(store.list(files.ROOT))
    except files.StoreUnavailableError as e:
        log.warning("event=orphan_sweep_skipped reason=store_down error=%s", type(e).__name__)
        return 0
    doomed = _candidates(stored, engine, now)
    if len(doomed) > max_deletes:
        log.warning("event=orphan_sweep_skipped reason=over_limit count=%d", len(doomed))
        return 0
    deleted = files.discard(store, doomed)
    log.info("event=orphans_swept count=%d", deleted)
    return deleted
