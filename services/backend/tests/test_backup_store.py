"""`kyapehnu backup` and `kyapehnu restore` through the picture store (LLD 3.4; AC-22, AC-31): the archive is the same one
whichever store holds the pictures, a backup made on one store restores onto a stack on the other (both ways), and a
restore that fails leaves nothing it wrote in the store and no database.

The checks that do not depend on the store (the whitelist of members, the refusals of a restore into a used stack) stay
in test_backup_restore.py, which runs on the disk store."""

import os
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.core.backup import backup, restore
from kyapehnu.core.cli import main
from kyapehnu.core.config import get_settings
from kyapehnu.core.db import prepare_database, utcnow
from kyapehnu.core.errors import ImportRefusedError
from kyapehnu.files import public as files
from kyapehnu.files.errors import StoreUnavailableError
from kyapehnu.files.local import LocalStore
from kyapehnu.files.s3 import S3Store
from kyapehnu.wardrobe.models import Item
from tests.conftest import LOOPBACK_PEER, SESSION_COOKIE, TEST_BUCKET, TEST_REGION
from tests.ingest_store_helpers import bucket_keys, put_fails_after

DB = "kyapehnu.db"
OLD_ARCHIVE = "kyapehnu-old.zip"


def keys_of(store) -> list[str]:
    return sorted(f.key for f in store.list(files.ROOT))


def build_stack(data_dir: Path, store) -> SimpleNamespace:
    """A stack two people have used: their accounts and items in the database at `data_dir`, and the pictures of those
    items (plus a mannequin each) in `store`. `pictures` maps key to bytes."""
    engine = prepare_database(data_dir)
    ids, pictures = [], {}
    with Session(engine) as s:
        for sub in ("a", "b"):
            user = upsert_google_user(s, sub=f"sub-{sub}", email=f"{sub}@example.com", name=sub, now=utcnow())
            ids.append(user.id)
            cutout, thumb = files.key(user.id, "cutout", f"{sub}1.png"), files.key(user.id, "thumb", f"{sub}1.webp")
            mannequin = files.key(user.id, "mannequin", f"{user.id}-{user.id + 1}.png")
            for key in (cutout, thumb, mannequin):
                pictures[key] = f"the bytes of {key}".encode() + os.urandom(8)
                store.put(key, pictures[key])
            s.add(Item(user_id=user.id, name=f"{sub} item", category="top", cutout_path=cutout, thumb_path=thumb))
        s.commit()
    engine.dispose()
    return SimpleNamespace(data_dir=data_dir, ids=ids, pictures=pictures)


def archive_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in z.namelist()}


def old_archive(data_dir: Path) -> Path:
    path = data_dir / "backups" / OLD_ARCHIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK an older backup")
    return path


def zips_in(data_dir: Path) -> list[str]:
    return sorted(p.name for p in (data_dir / "backups").iterdir())


# ---- backup -----------------------------------------------------------------------------------------------------


@pytest.fixture
def source(store, tmp_path) -> SimpleNamespace:
    """A used stack on `store`. For the disk store the pictures are NOT under the stack's own data dir, so a backup
    that walks `data_dir/images` instead of asking the store finds none."""
    return build_stack(tmp_path / "stack", store)


def test_the_archive_holds_the_database_and_exactly_the_pictures_of_the_store(source, store):
    archive = backup(source.data_dir, store)

    members = archive_members(archive)
    assert set(members) == {DB, *source.pictures}
    assert all(members[key] == data for key, data in source.pictures.items())
    with zipfile.ZipFile(archive) as z:
        assert {i.filename: i.compress_type for i in z.infolist() if i.filename != DB} == dict.fromkeys(
            source.pictures, zipfile.ZIP_STORED
        )  # pictures are already compressed


def test_an_object_that_is_not_a_picture_key_is_not_archived_and_the_archive_still_restores(
    source, store, plant, tmp_path
):
    for junk in ("images/1/cutout/.DS_Store", "images/1/thumb/notes.txt", "images/1/other/a.png", "readme.txt"):
        plant(junk, b"not ours")

    archive = backup(source.data_dir, store)

    assert set(archive_members(archive)) == {DB, *source.pictures}
    fresh = LocalStore(tmp_path / "fresh-images")
    restore(tmp_path / "fresh", fresh, archive)  # a stray file must not make a backup unrestorable
    assert keys_of(fresh) == sorted(source.pictures)


def test_a_backup_leaves_one_new_archive_and_no_temporary_file(source, store):
    old_archive(source.data_dir)

    archive = backup(source.data_dir, store)

    assert zips_in(source.data_dir) == sorted([OLD_ARCHIVE, archive.name])


def test_a_backup_that_loses_the_store_half_way_leaves_no_partial_archive_and_keeps_the_old_one(
    source, store, monkeypatch
):
    old = old_archive(source.data_dir)
    real_open, opened, seen_while_writing = type(store).open, [], []

    def open_(self, key):
        opened.append(key)
        if len(opened) == 2:
            seen_while_writing.extend(p.name for p in (source.data_dir / "backups").glob("*.zip"))
            raise StoreUnavailableError
        return real_open(self, key)

    monkeypatch.setattr(type(store), "open", open_)

    with pytest.raises(StoreUnavailableError):
        backup(source.data_dir, store)

    assert len(opened) == 2
    assert seen_while_writing == [OLD_ARCHIVE]  # the archive is written under another name and renamed when complete
    assert zips_in(source.data_dir) == [OLD_ARCHIVE]  # nothing partial, no temporary file
    assert old.read_bytes() == b"PK an older backup"


# ---- restore ----------------------------------------------------------------------------------------------------


@pytest.fixture
def archive(tmp_path) -> SimpleNamespace:
    """A backup of a used stack, made on the disk and copied out of it."""
    origin = tmp_path / "origin"
    built = build_stack(origin, LocalStore(origin))
    saved = tmp_path / "saved.zip"
    shutil.copy(backup(origin, LocalStore(origin)), saved)
    return SimpleNamespace(path=saved, pictures=built.pictures, ids=built.ids)


@pytest.fixture
def target(tmp_path) -> Path:
    """The data dir of a fresh stack."""
    return tmp_path / "fresh"


def test_a_restore_puts_every_picture_in_the_store_with_its_bytes_and_the_accounts_in_the_database(
    archive, store, target
):
    restore(target, store, archive.path)

    assert keys_of(store) == sorted(archive.pictures)
    assert all(store.read_bytes(key) == data for key, data in archive.pictures.items())
    engine = prepare_database(target)
    with Session(engine) as s:
        assert [i.name for i in s.exec(select(Item).order_by(Item.id)).all()] == ["a item", "b item"]
    engine.dispose()


def test_the_pictures_are_in_the_store_before_the_database_and_it_is_the_last_replace(
    archive, store, target, monkeypatch
):
    calls, real_replace = [], os.replace

    def spy(src, dst, *args, **kwargs):
        if Path(dst).name == DB:
            calls.append(("database", keys_of(store)))
        else:
            calls.append(("other", []))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)

    restore(target, store, archive.path)

    database_calls = [c for c in calls if c[0] == "database"]
    assert len(database_calls) == 1 and calls[-1] is database_calls[0]
    assert database_calls[0][1] == sorted(archive.pictures)  # every picture was already there


def test_a_restore_that_loses_the_store_half_way_removes_what_it_wrote_and_only_that(
    archive, store, target, monkeypatch, plant
):
    plant("images/9/thumb/keep.webp", b"already in the store")
    stored = put_fails_after(monkeypatch, 2, type(store))

    with pytest.raises(StoreUnavailableError):
        restore(target, store, archive.path)

    assert len(stored) == 2
    assert keys_of(store) == ["images/9/thumb/keep.webp"]
    assert not (target / DB).exists()  # no half-restored stack that a second run would refuse
    monkeypatch.undo()
    restore(target, store, archive.path)  # and the restore can be repeated
    assert keys_of(store) == sorted([*archive.pictures, "images/9/thumb/keep.webp"])


def test_a_restore_that_fails_at_the_last_step_removes_every_picture_it_wrote(archive, store, target, monkeypatch):
    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if Path(dst).name == DB:
            raise OSError("disk full")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)  # the database goes in last: the pictures are already there

    with pytest.raises(OSError, match="disk full"):
        restore(target, store, archive.path)

    assert keys_of(store) == []
    assert not (target / DB).exists()


def test_a_refused_archive_writes_nothing_to_the_store(archive, store, target, tmp_path):
    crafted = tmp_path / "crafted.zip"
    with zipfile.ZipFile(crafted, "w") as z:
        for name, data in {**archive_members(archive.path), "images/1/cutout/../../evil.png": b"x"}.items():
            z.writestr(name, data)

    with pytest.raises(ImportRefusedError):
        restore(target, store, crafted)

    assert keys_of(store) == []
    assert not (target / DB).exists()


# ---- AC-22: a backup of one store restores onto a stack on the other, both ways ----------------------------------


def use_stack(monkeypatch, data_dir: Path, *, bucket: bool) -> None:
    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KYAPEHNU_CONFIG_FILE", str(data_dir.parent / "config.yaml"))
    if bucket:
        monkeypatch.setenv("KYAPEHNU_FILES_BUCKET", TEST_BUCKET)
    else:
        monkeypatch.delenv("KYAPEHNU_FILES_BUCKET", raising=False)
    get_settings.cache_clear()


@pytest.mark.parametrize("direction", ["s3_to_local", "local_to_s3"])
def test_a_backup_made_on_one_store_restores_onto_a_fresh_stack_on_the_other_and_every_picture_is_served(
    direction, tmp_path, monkeypatch, s3, make_app, capsys
):
    from_s3 = direction.startswith("s3")
    origin, fresh = tmp_path / "origin", tmp_path / "fresh"
    origin_store = S3Store(TEST_BUCKET, TEST_REGION) if from_s3 else LocalStore(origin)
    built = build_stack(origin, origin_store)
    try:
        use_stack(monkeypatch, origin, bucket=from_s3)
        main(["backup"])
        [archive] = sorted((origin / "backups").glob("kyapehnu-*.zip"))
        if from_s3:  # the fresh stack is on the disk: empty the bucket the way a new machine would not have it
            s3.delete_objects(
                Bucket=TEST_BUCKET, Delete={"Objects": [{"Key": k} for k in built.pictures], "Quiet": True}
            )
        use_stack(monkeypatch, fresh, bucket=not from_s3)
        capsys.readouterr()

        main(["restore", str(archive)])
    finally:
        get_settings.cache_clear()

    assert capsys.readouterr().out == "restore complete\n"
    if from_s3:  # the fresh stack is on the disk
        assert keys_of(LocalStore(fresh)) == sorted(built.pictures)
        assert bucket_keys(s3) == []
    else:  # the fresh stack is on the bucket, and the disk holds no picture
        assert bucket_keys(s3) == sorted(built.pictures)
        assert not [p for p in (fresh / "images").rglob("*") if p.is_file()]
    app = make_app(data_dir=fresh, files_bucket=None if from_s3 else TEST_BUCKET)
    for key, data in built.pictures.items():
        owner = files.parse_key(key)[0]
        client = TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER)
        client.cookies.set(SESSION_COOKIE, app.state.sessions.create(owner))
        response = client.get(f"/{key}")
        assert (response.status_code, response.content) == (200, data), key
    app.state.ctx.engine.dispose()
