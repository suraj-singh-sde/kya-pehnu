"""The orphan sweep on the disk store (AC-19, LLD 3.5): what it removes and what it never touches. The same rules on
both stores, and the guards, are in test_sweep_store.py."""

import os
import time
from pathlib import Path

import pytest
from sqlmodel import Session

from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.core.config import DEFAULT_SWEEP_MAX_DELETES
from kyapehnu.core.db import utcnow
from kyapehnu.files.local import LocalStore
from kyapehnu.wardrobe.models import IngestJob, IngestPiece, Item, Outfit, Photo
from kyapehnu.wardrobe.sweep import ORPHAN_MIN_AGE_SECONDS
from kyapehnu.wardrobe.sweep import sweep_orphans as sweep_store
from tests.owner_helpers import write_image

NOW = 2_000_000_000.0
OLD = NOW - ORPHAN_MIN_AGE_SECONDS - 1
MANNEQUIN_RETENTION = 30 * 86400


def sweep_orphans(engine, data_dir, now=None, max_deletes=DEFAULT_SWEEP_MAX_DELETES):
    return sweep_store(engine, LocalStore(data_dir), max_deletes, now)


@pytest.fixture
def users(engine):
    """Two accounts (ids 1 and 2), each with a row: the sweep leaves a user with no rows alone (ADR-5)."""
    with Session(engine) as s:
        ids = [
            upsert_google_user(s, sub=f"sub-{i}", email=f"u{i}@example.com", name="n", now=utcnow()).id for i in (1, 2)
        ]
        s.add_all(Item(user_id=uid, name="no picture", category="top") for uid in ids)
        s.commit()
        return ids


def put(data_dir: Path, rel: str, mtime: float = OLD) -> Path:
    """A picture at data_dir/rel with the given modification time."""
    write_image(data_dir, rel)
    path = data_dir / rel
    os.utime(path, (mtime, mtime))
    return path


def test_an_unreferenced_old_file_of_every_kind_is_removed_in_every_users_directory(engine, tmp_path, users):
    paths = [
        put(tmp_path, f"images/{uid}/{kind}/orphan.{ext}")
        for uid in (1, 2)
        for kind, ext in (("original", "jpg"), ("cutout", "png"), ("thumb", "webp"), ("collage", "webp"))
    ]

    swept = sweep_orphans(engine, tmp_path, now=NOW)

    assert swept == len(paths)
    assert not [p for p in paths if p.exists()]


def test_files_that_a_row_refers_to_are_kept_whoever_the_owner(engine, tmp_path, users):
    rels = {
        "photo_orig": "images/1/original/o.jpg",
        "photo_norm": "images/1/original/o_norm.jpg",
        "item_cut": "images/2/cutout/c.png",
        "item_thumb": "images/2/thumb/c.webp",
        "item_alt_cut": "images/2/cutout/c2.png",
        "item_alt_thumb": "images/2/thumb/c2.webp",
        "piece_cut": "images/1/cutout/p.png",
        "piece_thumb": "images/1/thumb/p.webp",
        "piece_alt_cut": "images/1/cutout/p2.png",
        "piece_alt_thumb": "images/1/thumb/p2.webp",
        "collage": "images/2/collage/k.webp",
    }
    kept = [put(tmp_path, rel) for rel in rels.values()]
    with Session(engine) as s:
        photo = Photo(user_id=1, original_path=rels["photo_orig"], normalized_path=rels["photo_norm"])
        item = Item(
            user_id=2,
            name="i",
            category="top",
            cutout_path=rels["item_cut"],
            thumb_path=rels["item_thumb"],
            alt_cutout_path=rels["item_alt_cut"],
            alt_thumb_path=rels["item_alt_thumb"],
        )
        s.add_all([photo, item, Outfit(user_id=2, item_ids=[], item_key="", name="o", collage_path=rels["collage"])])
        s.flush()
        job = IngestJob(user_id=1, source="upload")
        s.add(job)
        s.flush()
        s.add(
            IngestPiece(
                user_id=1,
                job_id=job.id,
                photo_id=photo.id,
                cutout_path=rels["piece_cut"],
                thumb_path=rels["piece_thumb"],
                alt_cutout_path=rels["piece_alt_cut"],
                alt_thumb_path=rels["piece_alt_thumb"],
            )
        )
        s.commit()

    assert sweep_orphans(engine, tmp_path, now=NOW) == 0
    assert all(p.exists() for p in kept)


@pytest.mark.parametrize(
    ("age", "removed"),
    [(ORPHAN_MIN_AGE_SECONDS - 1, False), (ORPHAN_MIN_AGE_SECONDS, False), (ORPHAN_MIN_AGE_SECONDS + 1, True)],
)
def test_an_unreferenced_file_goes_only_after_an_hour(engine, tmp_path, users, age, removed):
    path = put(tmp_path, "images/1/thumb/young.webp", NOW - age)

    sweep_orphans(engine, tmp_path, now=NOW)

    assert path.exists() is not removed


def test_other_directories_under_images_are_left_alone(engine, tmp_path, users):
    kept = [
        put(tmp_path, "images/abc/original/x.jpg"),  # not a user directory
        put(tmp_path, "images/1/bogus/x.jpg"),  # not an image kind
        put(tmp_path, "images/1x/original/x.jpg"),
    ]

    assert sweep_orphans(engine, tmp_path, now=NOW) == 0
    assert all(p.exists() for p in kept)


def test_a_symlink_in_a_user_directory_is_neither_followed_nor_removed(engine, tmp_path, users):
    target = put(tmp_path, "elsewhere/secret.png")
    folder = tmp_path / "images" / "1" / "cutout"
    folder.mkdir(parents=True)
    link = folder / "link.png"
    link.symlink_to(target)

    assert sweep_orphans(engine, tmp_path, now=NOW) == 0
    assert link.is_symlink()
    assert target.exists()


def test_a_mannequin_of_a_saved_outfit_is_kept_per_user(engine, tmp_path, users):
    kept = put(tmp_path, "images/1/mannequin/1-2.png", NOW - MANNEQUIN_RETENTION - 10)
    other_users_same_ids = put(tmp_path, "images/2/mannequin/1-2.png", NOW - MANNEQUIN_RETENTION - 10)
    unsaved = put(tmp_path, "images/1/mannequin/3-4.png", NOW - MANNEQUIN_RETENTION - 10)
    recent = put(tmp_path, "images/1/mannequin/5-6.png", NOW - MANNEQUIN_RETENTION + 3600)
    with Session(engine) as s:
        s.add(Outfit(user_id=1, item_ids=[2, 1], item_key="1,2", name="saved"))
        s.commit()

    swept = sweep_orphans(engine, tmp_path, now=NOW)

    assert kept.exists()
    assert recent.exists()
    assert not unsaved.exists()
    assert not other_users_same_ids.exists()  # user 1's saved outfit protects only user 1's picture
    assert swept == 2


def test_a_data_dir_with_no_images_directory_sweeps_nothing(engine, tmp_path, users):
    assert sweep_orphans(engine, tmp_path / "empty", now=NOW) == 0


def test_the_default_clock_keeps_a_file_that_was_just_written(engine, tmp_path, users):
    path = put(tmp_path, "images/1/original/fresh.jpg", time.time())

    assert sweep_orphans(engine, tmp_path) == 0
    assert path.exists()
