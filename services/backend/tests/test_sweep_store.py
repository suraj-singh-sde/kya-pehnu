"""The orphan sweep on both stores and its guards (AC-18, AC-19, ADR-5, LLD 3.5).

`sweep_orphans(engine, store, max_deletes, now)` deletes a picture no row names once it is an hour old, and the
mannequin of an unsaved outfit once it is 30 days old. It is the safety net of a store that holds every user's
pictures, so it refuses to run on a doubtful picture of the world: a user with no rows, too many candidates, a store
that does not answer. Each refusal deletes nothing and is logged by reason."""

import logging
import os
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.core.db import utcnow
from kyapehnu.files.public import StoredFile, StoreUnavailableError
from kyapehnu.wardrobe.models import Item, Outfit
from kyapehnu.wardrobe.sweep import ORPHAN_MIN_AGE_SECONDS, sweep_orphans
from tests.conftest import LOOPBACK_PEER, TEST_BUCKET
from tests.fakes import add_item
from tests.owner_helpers import write_image

NOW = 2_000_000_000.0
HOUR = ORPHAN_MIN_AGE_SECONDS
OLD = 2 * HOUR
DAY = 86400
MANNEQUIN_RETENTION = 30 * DAY
CAP = 500


class Aged:
    """A store whose listing reports the ages given (a bucket's LastModified cannot be set); every other call, the
    deletes included, goes to the real store. `broken` keys refuse to be deleted."""

    def __init__(self, inner, ages, broken=()):
        self._inner, self._ages, self._broken = inner, ages, set(broken)

    def list(self, prefix):
        for found in self._inner.list(prefix):
            yield StoredFile(found.key, NOW - self._ages.get(found.key, 0))

    def delete(self, key):
        if key in self._broken:
            raise StoreUnavailableError
        self._inner.delete(key)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def sweep(engine, store, ages, *, cap=CAP, broken=()):
    return sweep_orphans(engine, Aged(store, ages, broken), cap, NOW)


@pytest.fixture
def users(engine):
    """Accounts 1 and 2, each with one row that names no picture, and account 3 who has no rows at all."""
    with Session(engine) as s:
        ids = [
            upsert_google_user(s, sub=f"sub-{i}", email=f"u{i}@example.com", name="n", now=utcnow()).id
            for i in (1, 2, 3)
        ]
        s.add_all(Item(user_id=uid, name="no picture", category="top") for uid in ids[:2])
        s.commit()
    return ids


@pytest.fixture
def plant_s3(s3):
    """`plant_s3(key)`: a picture in the bucket, put behind the store's back."""
    return lambda key: s3.put_object(Bucket=TEST_BUCKET, Key=key, Body=b"planted")


@pytest.fixture
def present(store, plant):
    """`present(*keys)`: which of these keys are in the store."""

    def _present(*keys):
        return [k for k in keys if store.exists(k)]

    return _present


def test_only_the_old_unreferenced_file_of_a_user_goes(engine, users, store, plant, present):
    kept, orphan, young, other = (
        "images/1/thumb/kept.webp",
        "images/1/thumb/orphan.webp",
        "images/1/thumb/young.webp",
        "images/2/thumb/other.webp",
    )
    for key in (kept, orphan, young, other):
        plant(key)
    with Session(engine) as s:
        s.add_all([Item(user_id=1, name="a", category="top", thumb_path=kept)])
        s.add_all([Item(user_id=2, name="b", category="top", thumb_path=other)])
        s.commit()
    ages = {kept: OLD, orphan: OLD, young: 600, other: OLD}

    swept = sweep(engine, store, ages)

    assert swept == 1
    assert present(kept, orphan, young, other) == [kept, young, other]


@pytest.mark.parametrize(
    ("age", "gone"), [(HOUR - 1, False), (HOUR, False), (HOUR + 1, True)], ids=["just_under", "exactly_1h", "over"]
)
def test_the_age_limit_is_an_hour_on_both_stores(engine, users, store, plant, present, age, gone):
    key = "images/1/cutout/maybe.png"
    plant(key)

    swept = sweep(engine, store, {key: age})

    assert (swept, bool(present(key))) == (int(gone), not gone)


def test_the_real_modification_time_of_the_store_is_what_the_sweep_reads(engine, users, store, plant, present):
    key = "images/1/original/just-written.jpg"
    plant(key)

    assert sweep_orphans(engine, store, CAP) == 0  # now is the real clock: the file is seconds old
    assert present(key) == [key]

    assert sweep_orphans(engine, store, CAP, time.time() + OLD) == 1  # two hours from now it is an orphan
    assert present(key) == []


def test_a_mannequin_goes_after_30_days_unless_a_saved_outfit_of_that_user_shows_it(
    engine, users, store, plant, present
):
    saved, unsaved, recent, others = (
        "images/1/mannequin/1-2.png",
        "images/1/mannequin/3-4.png",
        "images/1/mannequin/5-6.png",
        "images/2/mannequin/1-2.png",  # same item ids, but user 1's saved outfit protects only user 1's picture
    )
    old = MANNEQUIN_RETENTION + DAY
    for key in (saved, unsaved, recent, others):
        plant(key)
    with Session(engine) as s:
        s.add(Outfit(user_id=1, item_ids=[2, 1], item_key="1,2", name="saved"))
        s.commit()

    swept = sweep(engine, store, {saved: old, unsaved: old, recent: 10 * DAY, others: old})

    assert swept == 2
    assert present(saved, unsaved, recent, others) == [saved, recent]


# --- guard: no_rows_for_user ---


def test_a_user_with_no_rows_is_skipped_and_the_others_are_still_swept(engine, users, store, plant, present, caplog):
    orphan, theirs_a, theirs_b = "images/1/thumb/orphan.webp", "images/3/thumb/a.webp", "images/3/cutout/b.png"
    for key in (orphan, theirs_a, theirs_b):
        plant(key)
    caplog.set_level(logging.INFO)

    swept = sweep(engine, store, {orphan: OLD, theirs_a: OLD, theirs_b: OLD})

    assert swept == 1
    assert present(orphan, theirs_a, theirs_b) == [theirs_a, theirs_b]
    [skipped] = [m for m in caplog.messages if "event=orphan_sweep_skipped" in m]
    assert "reason=no_rows_for_user" in skipped and "user_id=3" in skipped and "objects=2" in skipped
    assert "images/" not in caplog.text and TEST_BUCKET not in caplog.text


def test_a_database_with_no_rows_at_all_deletes_nothing_from_a_store_full_of_pictures(
    engine, store, plant, present, caplog
):
    keys = [f"images/{uid}/thumb/p.webp" for uid in (1, 2, 3)]
    for key in keys:
        plant(key)
    caplog.set_level(logging.INFO)

    swept = sweep(engine, store, dict.fromkeys(keys, 10 * MANNEQUIN_RETENTION))  # a fresh or wrong database

    assert swept == 0
    assert present(*keys) == keys
    skipped = [m for m in caplog.messages if "event=orphan_sweep_skipped" in m and "reason=no_rows_for_user" in m]
    assert len(skipped) == 3


# --- guard: over_limit ---


def test_more_candidates_than_the_cap_deletes_nothing_and_says_how_many(engine, users, store, plant, present, caplog):
    orphans = ["images/1/thumb/o1.webp", "images/1/cutout/o2.png"]
    stale_mannequin = "images/2/mannequin/7-8.png"  # counts as a candidate too
    for key in (*orphans, stale_mannequin):
        plant(key)
    caplog.set_level(logging.INFO)
    ages = {orphans[0]: OLD, orphans[1]: OLD, stale_mannequin: MANNEQUIN_RETENTION + DAY}

    swept = sweep(engine, store, ages, cap=2)

    assert swept == 0
    assert present(*orphans, stale_mannequin) == [*orphans, stale_mannequin]
    [skipped] = [m for m in caplog.messages if "event=orphan_sweep_skipped" in m]
    assert "reason=over_limit" in skipped and "count=3" in skipped
    assert not any("event=orphans_swept" in m for m in caplog.messages)


def test_as_many_candidates_as_the_cap_are_all_deleted(engine, users, store, plant, present):
    orphans = ["images/1/thumb/o1.webp", "images/1/thumb/o2.webp", "images/2/cutout/o3.png"]
    kept = ["images/1/thumb/young.webp", "images/2/thumb/young.webp"]  # only candidates count against the cap
    for key in (*orphans, *kept):
        plant(key)

    swept = sweep(engine, store, {**dict.fromkeys(orphans, OLD), **dict.fromkeys(kept, 60)}, cap=3)

    assert swept == 3
    assert present(*orphans, *kept) == kept


# --- guard: store_down ---


def test_a_store_that_cannot_be_listed_gives_zero_and_a_logged_reason(engine, users, s3_store, s3_down, caplog):
    caplog.set_level(logging.INFO)

    assert sweep_orphans(engine, s3_store, CAP, NOW) == 0

    [skipped] = [m for m in caplog.messages if "event=orphan_sweep_skipped" in m]
    assert "reason=store_down" in skipped
    assert "images/" not in caplog.text and TEST_BUCKET not in caplog.text and "amazonaws" not in caplog.text


def test_a_refused_listing_is_the_same_skip_and_the_next_sweep_works_when_s3_is_back(
    engine, users, s3_store, s3_fail, plant_s3, caplog
):
    orphan = "images/1/thumb/orphan.webp"
    plant_s3(orphan)
    s3_fail("AccessDenied", "ListObjectsV2")
    caplog.set_level(logging.INFO)

    assert sweep(engine, s3_store, {orphan: OLD}) == 0
    assert any("reason=store_down" in m for m in caplog.messages)
    assert s3_store.exists(orphan)

    s3_fail.heal()
    assert sweep(engine, s3_store, {orphan: OLD}) == 1
    assert not s3_store.exists(orphan)


def test_an_app_starts_while_the_store_is_down(s3_app, s3_down, caplog):
    caplog.set_level(logging.INFO)

    with TestClient(s3_app, base_url="http://localhost", client=LOOPBACK_PEER) as c:
        assert c.get("/livez").status_code == 200

    assert any("reason=store_down" in m for m in caplog.messages)
    assert not any("event=housekeeping_failed" in m for m in caplog.messages)


# --- a delete that fails ---


def test_a_delete_that_fails_is_logged_by_class_and_the_sweep_goes_on(engine, users, store, plant, present, caplog):
    stuck, other = "images/1/thumb/stuck.webp", "images/1/thumb/other.webp"
    plant(stuck)
    plant(other)
    caplog.set_level(logging.INFO)

    sweep(engine, store, {stuck: OLD, other: OLD}, broken=[stuck])  # does not raise

    assert present(stuck, other) == [stuck]
    [failed] = [m for m in caplog.messages if "event=files_delete_failed" in m]
    assert "error=StoreUnavailableError" in failed
    assert "kind=" not in failed and "images/" not in caplog.text and "stuck" not in caplog.text


def test_a_refusing_bucket_on_delete_leaves_the_files_for_the_next_sweep(engine, users, s3_store, s3_fail, plant_s3):
    orphans = ["images/1/thumb/o1.webp", "images/1/thumb/o2.webp"]
    for key in orphans:
        plant_s3(key)
    s3_fail("AccessDenied", "DeleteObject")

    sweep(engine, s3_store, dict.fromkeys(orphans, OLD))  # does not raise

    assert all(s3_store.exists(k) for k in orphans)
    s3_fail.heal()
    assert sweep(engine, s3_store, dict.fromkeys(orphans, OLD)) == 2


# --- the cap comes from the settings ---


@pytest.mark.parametrize(("cap", "gone"), [(1, False), (2, True)], ids=["over", "at_the_cap"])
def test_the_app_sweeps_at_startup_with_the_cap_from_its_settings(make_app, cap, gone):
    app = make_app(sweep_max_deletes=cap)
    data_dir = app.state.ctx.data_dir
    orphans = []
    for n in (1, 2):
        rel = f"images/1/thumb/o{n}.webp"
        write_image(data_dir, rel)
        old = time.time() - OLD
        os.utime(data_dir / rel, (old, old))
        orphans.append(data_dir / rel)
    with Session(app.state.ctx.engine) as s:
        add_item(s)  # user 1 has a row, so the sweep does not skip them

    with TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER):
        pass

    assert [not p.exists() for p in orphans] == [gone, gone]
