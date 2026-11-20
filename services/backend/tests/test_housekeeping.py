"""Startup housekeeping (AC-47, D15): purge old LLM calls, sweep unreferenced image files, never block startup."""

import logging
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from kyapehnu.agent.models import LlmCall
from kyapehnu.core import housekeeping
from kyapehnu.core.config import DEFAULT_SWEEP_MAX_DELETES
from kyapehnu.core.db import make_engine
from kyapehnu.files.local import LocalStore
from kyapehnu.wardrobe.models import IngestJob, IngestPiece, Item, Outfit, Photo
from tests.chat_rows import with_owner
from tests.fakes import OWNER_ID, ensure_owner

NOW = 2_000_000_000.0  # fixed clock for sweep_orphans(now=...)
HOUR = 3600
DAY = 24 * HOUR


@pytest.fixture
def engine(engine):
    """User 1 with one row: the sweep leaves a user with no rows alone (ADR-5)."""
    with_owner(engine)
    with Session(engine) as s:
        s.add(Item(user_id=OWNER_ID, name="no picture", category="top"))
        s.commit()
    return engine


def sweep_orphans(engine, data_dir, now=None, max_deletes=DEFAULT_SWEEP_MAX_DELETES):
    return housekeeping.sweep_orphans(engine, LocalStore(data_dir), max_deletes, now)


def _file(data_dir: Path, rel: str, age_seconds: float, now: float = NOW) -> Path:
    path = data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (now - age_seconds, now - age_seconds))
    return path


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _call(created_at: datetime) -> LlmCall:
    return LlmCall(
        user_id=1,
        provider="fake",
        model="m",
        purpose="tagging",
        request_summary="s",
        latency_ms=1,
        created_at=created_at,
    )


# --- purge_llm_calls ---


def test_purge_deletes_only_older_than_30_days(engine):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        s.add_all([_call(now - timedelta(days=31)), _call(now - timedelta(days=29)), _call(now)])
        s.commit()

    assert housekeeping.purge_llm_calls(engine, now=now) == 1

    with Session(engine) as s:
        ages = sorted(now - _utc(c.created_at) for c in s.exec(select(LlmCall)).all())
    assert ages == [timedelta(0), timedelta(days=29)]


def test_purge_keeps_a_call_exactly_30_days_old(engine):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        s.add_all([_call(now - timedelta(days=30)), _call(now - timedelta(days=30, seconds=1))])
        s.commit()

    assert housekeeping.purge_llm_calls(engine, now=now) == 1

    with Session(engine) as s:
        [kept] = s.exec(select(LlmCall)).all()
    assert _utc(kept.created_at) == now - timedelta(days=30)


def test_purge_on_empty_table_returns_zero_and_defaults_now(engine):
    assert housekeeping.purge_llm_calls(engine) == 0
    with Session(engine) as s:
        s.add_all([_call(datetime.now(UTC) - timedelta(days=60)), _call(datetime.now(UTC))])
        s.commit()
    assert housekeeping.purge_llm_calls(engine) == 1


def test_purge_logs_count(engine, caplog):
    caplog.set_level(logging.INFO)
    with Session(engine) as s:
        s.add(_call(datetime.now(UTC) - timedelta(days=60)))
        s.commit()
    housekeeping.purge_llm_calls(engine)
    assert any("event=llm_calls_purged" in m and "count=1" in m for m in caplog.messages)


# --- sweep_orphans ---

REFERENCING = {
    "photo.original_path": "images/1/original/ref.jpg",
    "photo.normalized_path": "images/1/original/ref_norm.jpg",
    "item.cutout_path": "images/1/cutout/ref.png",
    "item.thumb_path": "images/1/thumb/ref.webp",
    "piece.cutout_path": "images/1/cutout/ref.png",
    "piece.thumb_path": "images/1/thumb/ref.webp",
    "outfit.collage_path": "images/1/collage/ref.webp",
}


def _reference(s: Session, column: str, rel: str) -> None:
    table, field = column.split(".")
    ensure_owner(s)
    if table == "item":
        s.add(Item(user_id=OWNER_ID, name="x", category="top", **{field: rel}))
        return
    if table == "outfit":
        s.add(Outfit(user_id=OWNER_ID, item_ids=[], item_key="ref", name="o", **{field: rel}))
        return
    job = IngestJob(user_id=OWNER_ID, source="upload")
    s.add(job)
    s.flush()
    if table == "photo":
        s.add(Photo(user_id=OWNER_ID, job_id=job.id, **{"original_path": "images/1/original/other.jpg", field: rel}))
        return
    photo = Photo(user_id=OWNER_ID, job_id=job.id, original_path="images/1/original/p.jpg")
    s.add(photo)
    s.flush()
    s.add(IngestPiece(user_id=OWNER_ID, job_id=job.id, photo_id=photo.id, **{field: rel}))


@pytest.mark.parametrize("column", list(REFERENCING))
def test_sweep_keeps_referenced_files_and_removes_the_unreferenced_one(engine, tmp_path, column):
    rel = REFERENCING[column]
    kind = rel.split("/")[2]  # images/<user>/<kind>/<name>
    keep = _file(tmp_path, rel, 2 * DAY)
    orphan = _file(tmp_path, f"images/1/{kind}/orphan.png", 2 * DAY)
    with Session(engine) as s:
        _reference(s, column, rel)
        s.commit()

    assert sweep_orphans(engine, tmp_path, now=NOW) == 1

    assert keep.exists() and not orphan.exists()


def test_sweep_deletes_old_unreferenced_files_in_all_three_dirs(engine, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    files = [_file(tmp_path, f"images/1/{kind}/orphan.png", 2 * DAY) for kind in ("original", "cutout", "thumb")]

    assert sweep_orphans(engine, tmp_path, now=NOW) == 3

    assert not any(f.exists() for f in files)
    assert any("event=orphans_swept" in m and "count=3" in m for m in caplog.messages)


def test_sweep_removes_an_old_collage_no_outfit_refers_to(engine, tmp_path):
    orphan = _file(tmp_path, "images/1/collage/orphan.webp", 2 * DAY)

    assert sweep_orphans(engine, tmp_path, now=NOW) == 1

    assert not orphan.exists()


def test_sweep_keeps_a_new_collage_that_no_outfit_refers_to_yet(engine, tmp_path):
    drawn_a_moment_ago = _file(
        tmp_path, "images/1/collage/new.webp", 60
    )  # a wear draws it before it commits the outfit

    assert sweep_orphans(engine, tmp_path, now=NOW) == 0

    assert drawn_a_moment_ago.exists()


@pytest.mark.parametrize(
    ("age", "swept"),
    [(0, False), (10 * 60, False), (HOUR, False), (HOUR + 1, True)],
    ids=["new", "ten_minutes", "exactly_one_hour", "just_over_one_hour"],
)
def test_sweep_only_deletes_files_older_than_one_hour(engine, tmp_path, age, swept):
    path = _file(tmp_path, "images/1/cutout/maybe.png", age)  # e.g. just written by a concurrent `kyapehnu seed`

    assert sweep_orphans(engine, tmp_path, now=NOW) == int(swept)

    assert path.exists() is not swept


def test_sweep_defaults_to_the_current_clock(engine, tmp_path):
    old = _file(tmp_path, "images/1/thumb/old.webp", 2 * DAY, now=time.time())
    young = _file(tmp_path, "images/1/thumb/young.webp", 60, now=time.time())

    assert sweep_orphans(engine, tmp_path) == 1

    assert not old.exists() and young.exists()


def test_sweep_ignores_files_outside_image_dirs(engine, tmp_path):
    outside = [
        _file(tmp_path, "notes.txt", 2 * DAY),
        _file(tmp_path, "backups/x.zip", 2 * DAY),
        _file(tmp_path, "images/1/other/stray.png", 2 * DAY),  # not one of original, cutout, thumb
        _file(tmp_path, "images/1/cutout/nested/deep.png", 2 * DAY),  # subdirectories are not walked
    ]
    old_db = tmp_path / "kyapehnu.db"
    os.utime(old_db, (NOW - 2 * DAY, NOW - 2 * DAY))

    assert sweep_orphans(engine, tmp_path, now=NOW) == 0

    assert all(p.exists() for p in outside) and old_db.exists()


def test_sweep_keeps_going_and_logs_the_class_when_one_delete_fails(engine, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    locked = _file(tmp_path, "images/1/cutout/locked.png", 2 * DAY)
    other = _file(tmp_path, "images/1/cutout/other.png", 2 * DAY)
    real_delete = LocalStore.delete

    def delete(self, key):
        if key.endswith("/locked.png"):
            raise PermissionError("in use")
        real_delete(self, key)

    monkeypatch.setattr(LocalStore, "delete", delete)

    sweep_orphans(engine, tmp_path, now=NOW)  # the count when a delete fails is not pinned (attempted or deleted)

    assert locked.exists() and not other.exists()
    assert any("event=files_delete_failed" in m and "error=PermissionError" in m for m in caplog.messages)
    assert "locked.png" not in caplog.text


def test_sweep_with_no_image_directories_sweeps_nothing(engine, tmp_path):
    shutil.rmtree(tmp_path / "images")  # the engine fixture creates the image directories

    assert sweep_orphans(engine, tmp_path, now=NOW) == 0


def test_sweep_never_follows_symlinks(engine, tmp_path):
    target = _file(tmp_path, "keep-me.txt", 2 * DAY)
    link = tmp_path / "images" / "1" / "thumb" / "link.webp"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")

    assert sweep_orphans(engine, tmp_path, now=NOW) == 0

    assert link.is_symlink() and target.exists()


# --- run ---


def test_run_purges_and_sweeps(engine, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    orphan = _file(tmp_path, "images/1/original/orphan.jpg", 2 * DAY, now=time.time())
    with Session(engine) as s:
        s.add(_call(datetime.now(UTC) - timedelta(days=60)))
        s.commit()

    housekeeping.run(engine, LocalStore(tmp_path), DEFAULT_SWEEP_MAX_DELETES)

    assert not orphan.exists()
    with Session(engine) as s:
        assert s.exec(select(LlmCall)).all() == []
    assert any("event=llm_calls_purged" in m for m in caplog.messages)
    assert any("event=orphans_swept" in m for m in caplog.messages)


def test_run_logs_and_continues_on_db_error(tmp_path, caplog):
    engine = make_engine(tmp_path)  # never migrated: every query fails with "no such table"
    caplog.set_level(logging.INFO)

    housekeeping.run(
        engine, LocalStore(tmp_path), DEFAULT_SWEEP_MAX_DELETES
    )  # must not raise: startup is never blocked

    assert any("event=housekeeping_failed" in m for m in caplog.messages)


@pytest.mark.parametrize("failing_step", ["purge_llm_calls", "sweep_orphans"])
def test_run_survives_an_error_in_either_step_and_still_runs_the_other(
    engine, tmp_path, monkeypatch, caplog, failing_step
):
    def boom(*_a, **_k):
        raise RuntimeError("disk gone")

    orphan = _file(tmp_path, "images/1/original/orphan.jpg", 2 * DAY, now=time.time())
    with Session(engine) as s:
        s.add(_call(datetime.now(UTC) - timedelta(days=60)))
        s.commit()
    monkeypatch.setattr(housekeeping, failing_step, boom)
    caplog.set_level(logging.INFO)

    housekeeping.run(engine, LocalStore(tmp_path), DEFAULT_SWEEP_MAX_DELETES)

    assert any("event=housekeeping_failed" in m and f"step={failing_step}" in m for m in caplog.messages)
    with Session(engine) as s:
        calls_left = len(s.exec(select(LlmCall)).all())
    if failing_step == "purge_llm_calls":
        assert calls_left == 1 and not orphan.exists()
    else:
        assert calls_left == 0 and orphan.exists()


def test_run_hands_the_delete_cap_to_the_sweep(engine, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    orphans = [_file(tmp_path, f"images/1/original/o{n}.jpg", 2 * DAY, now=time.time()) for n in range(3)]

    housekeeping.run(engine, LocalStore(tmp_path), 2)

    assert all(p.exists() for p in orphans)  # 3 candidates over a cap of 2: nothing is deleted
    assert any("event=orphan_sweep_skipped" in m and "reason=over_limit" in m for m in caplog.messages)


def test_run_with_the_store_down_still_purges_calls_and_never_raises(engine, tmp_path, monkeypatch, caplog):
    from kyapehnu.files.public import StoreUnavailableError

    def down(self, prefix):
        raise StoreUnavailableError

    monkeypatch.setattr(LocalStore, "list", down)
    caplog.set_level(logging.INFO)
    with Session(engine) as s:
        s.add(_call(datetime.now(UTC) - timedelta(days=60)))
        s.commit()

    housekeeping.run(engine, LocalStore(tmp_path), DEFAULT_SWEEP_MAX_DELETES)

    with Session(engine) as s:
        assert s.exec(select(LlmCall)).all() == []
    assert any("event=orphan_sweep_skipped" in m and "reason=store_down" in m for m in caplog.messages)
    assert not any("event=housekeeping_failed" in m for m in caplog.messages)  # a skipped sweep is not a failure
