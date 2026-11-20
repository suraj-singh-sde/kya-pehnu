"""The store-down rule (work plan, rules for every package) for the commands WP-5 owns: with the picture store
unreachable `backup`, `restore`, `import-phase3` and `copy-pictures` print one fixed line on stderr, exit 1 and leave
nothing half-done: no partial archive, no database or picture a restore wrote, no row or picture an import wrote, no
local picture deleted. Not a key, an address or a bucket name in what they print (NFR-6)."""

# ruff: noqa: F401, F811  (the Phase 3 fixtures `stack` and `make_source` are imported to be used as fixtures)

import shutil
from pathlib import Path

import pytest

from kyapehnu.core.cli import main
from kyapehnu.core.config import get_settings
from kyapehnu.files import public as files
from kyapehnu.files.errors import StoreUnavailableError
from kyapehnu.files.local import LocalStore
from kyapehnu.files.s3 import S3Store
from tests.conftest import TEST_BUCKET
from tests.ingest_store_helpers import bucket_keys, put_fails_after
from tests.test_backup_store import build_stack
from tests.test_import_phase3 import EMAIL_A, LAST_PHASE3, make_source, stack, stack_state

MESSAGE = "files_unavailable: Pictures are unavailable right now. Retry shortly.\n"
FORBIDDEN = (TEST_BUCKET, "amazonaws", "images/", EMAIL_A)
DB = "kyapehnu.db"
KEYS = [files.key(uid, kind, f"p{n}.png") for uid in (1, 2) for n, kind in enumerate(("cutout", "thumb", "original"))]


def on_the_bucket(monkeypatch, data_dir: Path) -> None:
    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KYAPEHNU_CONFIG_FILE", str(data_dir.parent / "config.yaml"))
    monkeypatch.setenv("KYAPEHNU_FILES_BUCKET", TEST_BUCKET)
    get_settings.cache_clear()


def down_line(argv: list[str], capsys) -> None:
    """The command stops with exit 1 and the one fixed line, and prints nothing else."""
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        main(argv)
    out, err = capsys.readouterr()
    assert stop.value.code == 1
    assert err == MESSAGE
    assert out == ""
    assert not any(secret in err for secret in FORBIDDEN)


def local_files(data_dir: Path) -> list[str]:
    return sorted(p.relative_to(data_dir).as_posix() for p in data_dir.rglob("*") if p.is_file())


@pytest.fixture
def used(tmp_path, monkeypatch, s3):
    """A stack on the bucket that two people have used: accounts, items and their pictures in the bucket."""
    data_dir = tmp_path / "stack"
    on_the_bucket(monkeypatch, data_dir)
    built = build_stack(data_dir, S3Store(TEST_BUCKET, "ap-south-1"))
    yield built
    get_settings.cache_clear()


@pytest.fixture
def archive(tmp_path) -> Path:
    """A backup of a stack that was on the disk, to restore into a fresh stack."""
    origin = tmp_path / "origin"
    build_stack(origin, LocalStore(origin))
    from kyapehnu.core.backup import backup

    saved = tmp_path / "saved.zip"
    shutil.copy(backup(origin, LocalStore(origin)), saved)
    return saved


# ---- backup -----------------------------------------------------------------------------------------------------


def test_backup_with_the_store_down_prints_the_line_exits_1_and_leaves_no_archive(used, s3_down, capsys):
    down_line(["backup"], capsys)

    assert list((used.data_dir / "backups").glob("*")) == []


def test_backup_that_loses_the_store_half_way_leaves_no_partial_archive(used, capsys, monkeypatch):
    opened = []
    real_open = S3Store.open

    def open_(self, key):
        opened.append(key)
        if len(opened) == 2:
            raise StoreUnavailableError
        return real_open(self, key)

    monkeypatch.setattr(S3Store, "open", open_)

    down_line(["backup"], capsys)

    assert len(opened) == 2
    assert list((used.data_dir / "backups").glob("*")) == []


def test_backup_still_works_on_the_bucket(used, capsys):
    capsys.readouterr()

    main(["backup"])

    assert capsys.readouterr().out.startswith("backup written: ")
    [archive] = (used.data_dir / "backups").glob("kyapehnu-*.zip")
    assert archive.stat().st_size > 0


# ---- restore ----------------------------------------------------------------------------------------------------


@pytest.fixture
def fresh(tmp_path, monkeypatch, s3) -> Path:
    """The data dir of a fresh stack on the bucket, with no database yet."""
    data_dir = tmp_path / "fresh"
    on_the_bucket(monkeypatch, data_dir)
    yield data_dir
    get_settings.cache_clear()


def test_restore_with_the_store_down_prints_the_line_exits_1_and_leaves_no_database_and_no_picture(
    fresh, archive, s3, s3_down, capsys
):
    down_line(["restore", str(archive)], capsys)

    assert not (fresh / DB).exists()
    assert not list(fresh.glob(".restore-*"))  # no staging folder left behind
    s3_down.heal()
    assert bucket_keys(s3) == []
    main(["restore", str(archive)])  # and the restore can be repeated
    assert (fresh / DB).exists()
    assert len(bucket_keys(s3)) == 6


def test_restore_that_loses_the_store_half_way_discards_what_it_put_and_leaves_no_database(
    fresh, archive, s3, capsys, monkeypatch
):
    stored = put_fails_after(monkeypatch, 3)

    down_line(["restore", str(archive)], capsys)

    assert len(stored) == 3
    assert bucket_keys(s3) == []  # the three that went in were taken back
    assert not (fresh / DB).exists()


# ---- import-phase3 ----------------------------------------------------------------------------------------------


@pytest.fixture
def phase3(stack, make_source, monkeypatch, s3):
    """The two-account stack of the import tests on the bucket, and a Phase 3 source to import."""
    monkeypatch.setenv("KYAPEHNU_FILES_BUCKET", TEST_BUCKET)
    get_settings.cache_clear()
    return stack, make_source(LAST_PHASE3)


def test_import_phase3_with_the_store_down_prints_the_line_exits_1_and_adds_no_row(phase3, s3, s3_down, capsys):
    stack, source = phase3
    before = stack_state(stack)

    down_line(["import-phase3", "--source", str(source.root), "--email", EMAIL_A], capsys)

    assert stack_state(stack) == before
    s3_down.heal()
    assert bucket_keys(s3) == []


def test_import_phase3_that_loses_the_store_half_way_discards_what_it_put_and_adds_no_row(
    phase3, s3, capsys, monkeypatch
):
    stack, source = phase3
    before = stack_state(stack)
    stored = put_fails_after(monkeypatch, 5)

    down_line(["import-phase3", "--source", str(source.root), "--email", EMAIL_A], capsys)

    assert len(stored) == 5
    assert bucket_keys(s3) == []
    assert stack_state(stack) == before


# ---- copy-pictures ----------------------------------------------------------------------------------------------


@pytest.fixture
def pictures(tmp_path, monkeypatch, s3) -> Path:
    """A data dir whose disk holds six pictures, with the bucket named in the environment."""
    data_dir = tmp_path / "data"
    on_the_bucket(monkeypatch, data_dir)
    local = LocalStore(data_dir)
    for key in KEYS:
        local.put(key, f"the bytes of {key}".encode())
    yield data_dir
    get_settings.cache_clear()


@pytest.mark.parametrize("argv", [["--to", "s3"], ["--to", "s3", "--delete-source"], ["--to", "local"]])
def test_copy_pictures_with_the_store_down_prints_the_line_exits_1_and_deletes_nothing(
    pictures, s3, s3_down, capsys, argv
):
    down_line(["copy-pictures", *argv], capsys)

    assert local_files(pictures) == sorted(KEYS)
    s3_down.heal()
    assert bucket_keys(s3) == []
