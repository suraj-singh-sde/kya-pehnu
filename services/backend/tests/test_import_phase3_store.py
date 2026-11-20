"""`kyapehnu import-phase3` on the picture store (LLD 3.4; AC-23, AC-31): the Phase 3 pictures are written through the
store under the folder of the account, on both stores, and a failure (rows or store) leaves no picture in the store and
no row in the database. The source layout, the rows and the refusals are tested in test_import_phase3.py on the disk."""

# ruff: noqa: F401, F811  (the Phase 3 fixtures `stack` and `make_source` are imported to be used as fixtures)

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kyapehnu.core import import_phase3
from kyapehnu.core.cli import main
from kyapehnu.core.config import get_settings
from kyapehnu.core.errors import ImportRefusedError
from kyapehnu.files import public as files
from kyapehnu.files.errors import StoreUnavailableError
from tests.conftest import LOOPBACK_PEER, SESSION_COOKIE, TEST_BUCKET
from tests.ingest_store_helpers import bucket_keys, put_fails_after
from tests.test_import_phase3 import EMAIL_A, LAST_PHASE3, edit_db, make_source, stack, stack_state


def keys_of(store) -> list[str]:
    return sorted(f.key for f in store.list(files.ROOT))


def expected_keys(source, user_id: int) -> dict[str, bytes]:
    """The key every source file gets: images/<kind>/<name> becomes images/<user id>/<kind>/<name>."""
    return {rel.replace("images/", f"images/{user_id}/", 1): data for rel, data in source.files.items()}


def picture_files(data_dir: Path) -> list[Path]:
    return [p for p in (data_dir / "images").rglob("*") if p.is_file()]


def test_every_picture_of_the_source_is_put_in_the_store_under_the_folder_of_the_account(
    stack,
    make_source,
    store,
):
    source = make_source(LAST_PHASE3)
    wanted = expected_keys(source, stack.a)

    counts = import_phase3.run(get_settings(), store, source.root, EMAIL_A)

    assert keys_of(store) == sorted(wanted)
    assert all(store.read_bytes(key) == data for key, data in wanted.items())
    assert (counts.images, counts.missing_images) == (len(wanted), 1)
    assert picture_files(stack.data_dir) == []  # nothing went around the store


def test_on_the_bucket_the_command_stores_the_pictures_there_and_the_app_serves_them(
    stack,
    make_source,
    s3,
    make_app,
    monkeypatch,
    capsys,
):
    source = make_source(LAST_PHASE3)
    wanted = expected_keys(source, stack.a)
    monkeypatch.setenv("KYAPEHNU_FILES_BUCKET", TEST_BUCKET)
    get_settings.cache_clear()
    capsys.readouterr()

    main(["import-phase3", "--source", str(source.root), "--email", EMAIL_A])

    assert f"images={len(wanted)} missing_images=1" in capsys.readouterr().out
    assert bucket_keys(s3) == sorted(wanted)
    assert picture_files(stack.data_dir) == []
    app = make_app(data_dir=stack.data_dir, files_bucket=TEST_BUCKET)
    client = TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER)
    client.cookies.set(SESSION_COOKIE, app.state.sessions.create(stack.a))
    for key in (f"images/{stack.a}/cutout/it1.png", f"images/{stack.a}/mannequin/1-2-3.png"):
        response = client.get(f"/{key}")
        assert (response.status_code, response.content) == (200, wanted[key]), key
    app.state.ctx.engine.dispose()


def test_a_failure_while_writing_the_rows_removes_the_pictures_from_the_store_and_adds_no_row(
    stack,
    make_source,
    store,
    monkeypatch,
):
    insert = import_phase3._insert_table

    def fail_on_the_last_table(con, table, user_id):
        if table == "feedback":
            raise sqlite3.OperationalError("disk I/O error")
        return insert(con, table, user_id)

    monkeypatch.setattr(import_phase3, "_insert_table", fail_on_the_last_table)
    source = make_source(LAST_PHASE3)
    before = stack_state(stack)

    with pytest.raises(sqlite3.OperationalError):
        import_phase3.run(get_settings(), store, source.root, EMAIL_A)

    assert keys_of(store) == []
    assert stack_state(stack) == before


def test_a_store_that_goes_down_half_way_leaves_no_picture_and_no_row_and_the_import_can_be_repeated(
    stack,
    make_source,
    store,
    monkeypatch,
):
    source = make_source(LAST_PHASE3)
    before = stack_state(stack)
    stored = put_fails_after(monkeypatch, 5, type(store))

    with pytest.raises(StoreUnavailableError):
        import_phase3.run(get_settings(), store, source.root, EMAIL_A)

    assert len(stored) == 5
    assert keys_of(store) == []  # the five that went in were taken back
    assert stack_state(stack) == before
    monkeypatch.undo()
    counts = import_phase3.run(get_settings(), store, source.root, EMAIL_A)
    assert counts.rows["items"] == 32
    assert keys_of(store) == sorted(expected_keys(source, stack.a))


def test_a_refused_source_writes_nothing_to_the_store(stack, make_source, store):
    source = make_source(
        LAST_PHASE3,
        tamper=lambda root: edit_db(root, "UPDATE item SET cutout_path = ? WHERE id = 1", ("images/../../x.png",)),
    )

    with pytest.raises(ImportRefusedError):
        import_phase3.run(get_settings(), store, source.root, EMAIL_A)

    assert keys_of(store) == []
