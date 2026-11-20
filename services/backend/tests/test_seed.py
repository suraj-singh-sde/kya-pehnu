"""`seed` fills one account's wardrobe, with its pictures under that account's image directories (LLD 3.4, 3.6, 3.8)."""

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.core.db import utcnow
from kyapehnu.files.errors import StoreUnavailableError
from kyapehnu.files.local import LocalStore
from kyapehnu.wardrobe import public as wardrobe
from kyapehnu.wardrobe.models import Item
from kyapehnu.wardrobe.public import SeedError
from kyapehnu.wardrobe.seed import SEED_ITEMS
from tests.owner_helpers import add_item, image_files, rows_of


class Ctx:
    """The two things `seed` needs from the app context."""

    def __init__(self, engine, data_dir):
        self.engine, self.data_dir = engine, data_dir
        self.files = LocalStore(data_dir)


@pytest.fixture
def ctx(engine, tmp_path):
    with Session(engine) as s:
        for i in (1, 2):
            upsert_google_user(s, sub=f"sub-{i}", email=f"u{i}@example.com", name="n", now=utcnow())
    return Ctx(engine, tmp_path)


def test_seed_adds_the_demo_items_to_the_named_user_only(ctx):
    assert wardrobe.seed(2, ctx.engine, ctx.files) == len(SEED_ITEMS)

    assert len(rows_of(ctx, Item, 2)) == len(SEED_ITEMS)
    assert rows_of(ctx, Item, 1) == []


def test_seed_pictures_live_in_the_users_directories_and_are_the_ones_the_rows_name(ctx):
    wardrobe.seed(2, ctx.engine, ctx.files)

    files = image_files(ctx.data_dir)
    rows = rows_of(ctx, Item, 2)
    assert files
    assert all(f.startswith("images/2/") for f in files)
    for row in rows:
        assert row.cutout_path.startswith("images/2/cutout/")
        assert row.thumb_path.startswith("images/2/thumb/")
        assert (ctx.data_dir / row.cutout_path).is_file()
        assert (ctx.data_dir / row.thumb_path).is_file()


def test_each_user_can_be_seeded_once_without_force(ctx):
    wardrobe.seed(1, ctx.engine, ctx.files)

    assert wardrobe.seed(2, ctx.engine, ctx.files) == len(SEED_ITEMS)
    assert len(rows_of(ctx, Item, 1)) == len(SEED_ITEMS)
    assert len(rows_of(ctx, Item, 2)) == len(SEED_ITEMS)


def test_seed_refuses_a_user_who_has_items_and_writes_nothing(ctx):
    add_item(ctx, 2, name="mine")
    before = image_files(ctx.data_dir)

    with pytest.raises(SeedError):
        wardrobe.seed(2, ctx.engine, ctx.files)

    assert len(rows_of(ctx, Item, 2)) == 1
    assert image_files(ctx.data_dir) == before


def test_another_users_items_do_not_block_seeding(ctx):
    add_item(ctx, 1, name="theirs")

    assert wardrobe.seed(2, ctx.engine, ctx.files) == len(SEED_ITEMS)


def test_force_adds_the_demo_items_again_for_that_user(ctx):
    wardrobe.seed(2, ctx.engine, ctx.files)

    assert wardrobe.seed(2, ctx.engine, ctx.files, force=True) == len(SEED_ITEMS)
    assert len(rows_of(ctx, Item, 2)) == 2 * len(SEED_ITEMS)
    assert rows_of(ctx, Item, 1) == []


# --- on every store, and when the store or the commit fails (AC-23, LLD 4.4) ----------------------------------------


def _stored_keys(store) -> list[str]:
    return sorted(f.key for f in store.list("images/"))


def test_seed_puts_every_picture_a_row_names_in_the_store(engine, store, ctx):
    assert wardrobe.seed(2, ctx.engine, store) == len(SEED_ITEMS)

    keys = [key for row in rows_of(ctx, Item, 2) for key in (row.cutout_path, row.thumb_path)]
    assert _stored_keys(store) == sorted(keys)  # every named key is there and nothing else is
    assert all(store.exists(key) for key in keys)


def test_a_store_that_fails_half_way_adds_no_rows_and_takes_back_what_was_put(engine, store, ctx, monkeypatch):
    original, calls = type(store).put, []

    def put(self, key, body):
        if len(calls) == 7:
            raise StoreUnavailableError
        calls.append(key)
        original(self, key, body)

    monkeypatch.setattr(type(store), "put", put)

    with pytest.raises(StoreUnavailableError):
        wardrobe.seed(2, ctx.engine, store)

    assert len(calls) == 7
    assert rows_of(ctx, Item, 2) == []
    assert _stored_keys(store) == []


def test_a_failed_commit_takes_back_every_picture(engine, store, ctx, monkeypatch):
    original, calls = Session.commit, []

    def commit(self):
        calls.append(1)
        raise OperationalError("COMMIT", {}, Exception("database is locked"))

    monkeypatch.setattr(Session, "commit", commit)

    with pytest.raises(OperationalError):
        wardrobe.seed(2, ctx.engine, store)

    monkeypatch.setattr(Session, "commit", original)
    assert calls == [1]
    assert rows_of(ctx, Item, 2) == []
    assert _stored_keys(store) == []
