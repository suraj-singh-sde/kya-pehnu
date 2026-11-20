import errno
import re
import sqlite3
import zipfile

import pytest
from PIL import Image
from sqlmodel import Session, select

from kyapehnu.agent.models import LlmCall
from kyapehnu.core import housekeeping
from kyapehnu.core.backup import backup
from kyapehnu.core.config import DEFAULT_SWEEP_MAX_DELETES
from kyapehnu.core.schemas import GarmentTags
from kyapehnu.files.local import LocalStore
from kyapehnu.wardrobe.models import Category, Item, Pattern, Season
from kyapehnu.wardrobe.seed import SEED_ITEMS, SeedError, draw_silhouette, seed
from tests.fakes import OWNER_ID, ensure_owner


@pytest.fixture(autouse=True)
def _owner(engine):
    with Session(engine) as s:
        ensure_owner(s)
        s.commit()


def _items(engine):
    with Session(engine) as s:
        return s.exec(select(Item)).all()


# --- seed (AC-32) ---


def test_seed_items_are_valid_and_cover_categories_formality_and_seasons():
    assert len(SEED_ITEMS) >= 30
    for raw in SEED_ITEMS:
        GarmentTags.model_validate(raw)
    assert {r["category"] for r in SEED_ITEMS} == {c.value for c in Category}
    assert {r["formality"] for r in SEED_ITEMS} == {1, 2, 3, 4, 5}
    assert {season for r in SEED_ITEMS for season in r["seasons"]} == {s.value for s in Season}


def test_seed_creates_items_with_images_and_no_ai_tags_or_llm_calls(engine, tmp_path):
    n = seed(OWNER_ID, engine, LocalStore(tmp_path))

    items = _items(engine)
    assert n == len(items) == len(SEED_ITEMS)
    assert all(i.ai_tags is None and i.phash and i.photo_id is None for i in items)
    assert all((tmp_path / i.cutout_path).is_file() and (tmp_path / i.thumb_path).is_file() for i in items)
    with Session(engine) as s:
        assert s.exec(select(LlmCall)).all() == []


def test_seed_refuses_when_items_exist_and_writes_nothing(engine, tmp_path):
    seed(OWNER_ID, engine, LocalStore(tmp_path))
    files_before = sorted(p.name for p in (tmp_path / "images" / "1" / "thumb").iterdir())

    with pytest.raises(SeedError):
        seed(OWNER_ID, engine, LocalStore(tmp_path))

    assert len(_items(engine)) == len(SEED_ITEMS)
    assert sorted(p.name for p in (tmp_path / "images" / "1" / "thumb").iterdir()) == files_before


def test_seed_force_adds_items_again(engine, tmp_path):
    seed(OWNER_ID, engine, LocalStore(tmp_path))
    assert seed(OWNER_ID, engine, LocalStore(tmp_path), force=True) == len(SEED_ITEMS)
    assert len(_items(engine)) == 2 * len(SEED_ITEMS)


def test_seed_files_are_referenced_so_the_orphan_sweep_keeps_them(engine, tmp_path):
    seed(OWNER_ID, engine, LocalStore(tmp_path))

    swept = housekeeping.sweep_orphans(
        engine, LocalStore(tmp_path), DEFAULT_SWEEP_MAX_DELETES, now=4_000_000_000.0
    )  # far future: every file is "old"

    assert swept == 0
    assert all((tmp_path / i.thumb_path).exists() for i in _items(engine))


@pytest.mark.parametrize("category", sorted(c.value for c in Category))
def test_draw_silhouette_paints_something_on_a_transparent_canvas(category):
    im = draw_silhouette(category, "#1F2A44", "solid")

    assert im.mode == "RGBA" and im.size == (512, 512)
    box = im.getchannel("A").getbbox()
    assert box is not None and box != (0, 0, 512, 512)  # drawn shape, transparent surroundings


@pytest.mark.parametrize("pattern", sorted(p.value for p in Pattern))
def test_draw_silhouette_supports_every_pattern(pattern):
    assert isinstance(draw_silhouette("top", "#1F2A44", pattern), Image.Image)


# --- backup (AC-33) ---


def test_backup_zip_has_consistent_db_and_images(engine, tmp_path):
    seed(OWNER_ID, engine, LocalStore(tmp_path))
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "old.zip").write_bytes(b"old")

    out = backup(tmp_path, LocalStore(tmp_path))

    assert out.is_file() and out.parent == tmp_path / "backups"
    assert re.fullmatch(r"kyapehnu-\d{8}-\d{6}\.zip", out.name)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert "kyapehnu.db" in names
        assert any(n.startswith("images/1/thumb/") for n in names)
        assert not any(n.startswith("backups/") for n in names)
        z.extract("kyapehnu.db", tmp_path / "restore")
    con = sqlite3.connect(tmp_path / "restore" / "kyapehnu.db")
    try:
        assert con.execute("select count(*) from item").fetchone()[0] == len(SEED_ITEMS)
        assert con.execute("pragma integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_backup_contains_exactly_the_db_and_every_image(engine, tmp_path):
    images = {
        "images/1/original/a.jpg": b"original",
        "images/1/cutout/a.png": b"cutout",
        "images/2/thumb/a.webp": b"thumb",
    }
    for rel, data in images.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(data)
    (tmp_path / "notes.txt").write_text("not part of a backup")
    (tmp_path / "images" / "1" / "cutout" / ".DS_Store").write_bytes(b"a file no restore would accept")

    with zipfile.ZipFile(backup(tmp_path, LocalStore(tmp_path))) as z:
        assert set(z.namelist()) == {"kyapehnu.db", *images}  # no -wal/-shm files, no stray files, no absolute paths
        assert {rel: z.read(rel) for rel in images} == images


def test_backup_includes_committed_rows_still_in_the_wal(engine, tmp_path):
    with Session(engine) as s:
        s.add(Item(user_id=OWNER_ID, name="Fresh", category="top"))
        s.commit()  # in WAL mode this may not be in kyapehnu.db yet: the snapshot must still contain it

    with zipfile.ZipFile(backup(tmp_path, LocalStore(tmp_path))) as z:
        z.extract("kyapehnu.db", tmp_path / "restore")
    con = sqlite3.connect(tmp_path / "restore" / "kyapehnu.db")
    try:
        assert con.execute("select name from item").fetchall() == [("Fresh",)]
    finally:
        con.close()


def test_failed_backup_removes_the_partial_zip_and_reraises(engine, tmp_path, monkeypatch):
    (tmp_path / "images" / "1" / "thumb").mkdir(parents=True)
    (tmp_path / "images" / "1" / "thumb" / "a.webp").write_bytes(b"thumb")

    def open_fails(self, key):  # the archive is already being written, with the DB in it, when an image fails
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(LocalStore, "open", open_fails)

    with pytest.raises(OSError):
        backup(tmp_path, LocalStore(tmp_path))

    assert list((tmp_path / "backups").iterdir()) == []


def test_backup_never_contains_the_key_file(engine, tmp_path):
    from kyapehnu.agent.llm import store

    store.save(
        tmp_path,
        store.LlmStore(active="openai", providers={"openai": store.ProviderSecret(api_key="sk-SECRET", model="m")}),
    )

    out = backup(tmp_path, LocalStore(tmp_path))

    with zipfile.ZipFile(out) as z:
        assert store.FILE_NAME not in z.namelist()
        assert all(b"sk-SECRET" not in z.read(n) for n in z.namelist())
