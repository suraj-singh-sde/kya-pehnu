"""`kyapehnu copy-pictures` (LLD 3.7, ADR-6; AC-20, AC-21): the one-time move of pictures between the disk and the bucket,
and back. `copy_all` over two stores, then the command: its output line, its refusals and its exit codes.

Equal bytes in the target count as present, different bytes as failed (never overwritten), a source that cannot be read
as failed, and `--delete-source` deletes only after the target's checksum was read again."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.core.cli import main
from kyapehnu.core.config import get_settings
from kyapehnu.core.db import prepare_database, utcnow
from kyapehnu.files import public as files
from kyapehnu.files.copy import CopyCounts, copy_all
from kyapehnu.files.errors import MissingFileError, StoreUnavailableError
from kyapehnu.files.local import LocalStore
from tests.conftest import TEST_BUCKET
from tests.ingest_store_helpers import bucket_keys, put_fails_after

KEYS = [
    files.key(uid, kind, f"p{n}.{ext}")
    for uid in (1, 2)
    for n, (kind, ext) in enumerate([("original", "jpg"), ("cutout", "png"), ("thumb", "webp")])
]
N = len(KEYS)
BAD_CHECKSUM = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
REFUSED = 2
FAILED = 1


def data_for(key: str) -> bytes:
    return f"the bytes of {key}".encode()


def keys_of(store) -> list[str]:
    return sorted(f.key for f in store.list(files.ROOT))


def plant(store, keys=KEYS) -> None:
    for key in keys:
        store.put(key, data_for(key))


# ---- copy_all: two stores ---------------------------------------------------------------------------------------


@pytest.fixture(params=["to_s3", "to_local"])
def pair(request, local_dir, s3_store):
    """The disk and the bucket, as (source, target) in the direction under test."""
    local = LocalStore(local_dir)
    source, target = (local, s3_store) if request.param == "to_s3" else (s3_store, local)
    plant(source)
    return SimpleNamespace(source=source, target=target)


def test_every_picture_of_both_users_is_copied_with_equal_bytes_and_the_second_run_finds_them_present(pair):
    first = copy_all(pair.source, pair.target)
    second = copy_all(pair.source, pair.target)

    assert first == CopyCounts(copied=N, present=0, failed=0, deleted=0)
    assert second == CopyCounts(copied=0, present=N, failed=0, deleted=0)
    assert keys_of(pair.target) == sorted(KEYS)
    for key in KEYS:
        assert pair.target.read_bytes(key) == data_for(key)
    assert keys_of(pair.source) == sorted(KEYS)  # a copy takes nothing from the source


def test_a_target_with_different_bytes_is_failed_and_never_overwritten(pair):
    pair.target.put(KEYS[0], b"somebody else's bytes")

    counts = copy_all(pair.source, pair.target)

    assert counts == CopyCounts(copied=N - 1, present=0, failed=1, deleted=0)
    assert pair.target.read_bytes(KEYS[0]) == b"somebody else's bytes"
    assert pair.source.read_bytes(KEYS[0]) == data_for(KEYS[0])


@pytest.mark.parametrize("delete_source", [False, True])
def test_a_picture_that_cannot_be_read_is_one_failure_and_the_rest_are_copied(pair, monkeypatch, delete_source):
    unreadable, real_open = KEYS[3], pair.source.open

    def open_(key):
        if key == unreadable:
            raise PermissionError("not for this process")
        return real_open(key)

    monkeypatch.setattr(pair.source, "open", open_)

    counts = copy_all(pair.source, pair.target, delete_source=delete_source)

    assert (counts.copied, counts.failed) == (N - 1, 1)
    assert keys_of(pair.target) == sorted(set(KEYS) - {unreadable})
    # no source picture is deleted for the failed one; without the flag none is deleted at all
    assert keys_of(pair.source) == (sorted([unreadable]) if delete_source else sorted(KEYS))
    assert counts.deleted == (N - 1 if delete_source else 0)


def test_with_delete_source_a_copied_picture_is_removed_from_the_source_after_it_was_copied(pair):
    counts = copy_all(pair.source, pair.target, delete_source=True)

    assert counts == CopyCounts(copied=N, present=0, failed=0, deleted=N)
    assert keys_of(pair.source) == []
    assert keys_of(pair.target) == sorted(KEYS)
    for key in KEYS:
        assert pair.target.read_bytes(key) == data_for(key)


def test_with_delete_source_a_picture_that_is_already_present_is_removed_from_the_source_too(pair):
    copy_all(pair.source, pair.target)

    counts = copy_all(pair.source, pair.target, delete_source=True)

    assert counts == CopyCounts(copied=0, present=N, failed=0, deleted=N)
    assert keys_of(pair.source) == []


@pytest.mark.parametrize("reread", ["other-hash", "gone"])
def test_the_source_is_kept_when_the_checksum_read_again_after_the_copy_does_not_match(pair, monkeypatch, reread):
    suspect, real_checksum, reads = KEYS[1], pair.target.checksum, {"n": 0}

    def checksum(key):
        if key != suspect:
            return real_checksum(key)
        reads["n"] += 1
        if reads["n"] == 1:  # the compare before the copy
            return real_checksum(key)
        if reread == "gone":
            raise MissingFileError
        return BAD_CHECKSUM

    monkeypatch.setattr(pair.target, "checksum", checksum)

    counts = copy_all(pair.source, pair.target, delete_source=True)

    assert reads["n"] == 2  # compared once before and read once more before the delete
    assert (counts.failed, counts.deleted) == (1, N - 1)
    assert keys_of(pair.source) == [suspect]


def test_a_failed_put_is_counted_as_that_pictures_failure_and_the_run_goes_on(pair, monkeypatch):
    real_put, put = pair.target.put, []

    def flaky_put(key, body):
        if key == KEYS[2]:
            raise StoreUnavailableError  # for example BadDigest from S3
        put.append(key)
        real_put(key, body)

    monkeypatch.setattr(pair.target, "put", flaky_put)

    counts = copy_all(pair.source, pair.target, delete_source=True)

    assert (counts.copied, counts.failed, counts.deleted) == (N - 1, 1, N - 1)
    assert keys_of(pair.source) == [KEYS[2]]


# ---- the command ------------------------------------------------------------------------------------------------


@pytest.fixture
def stack(tmp_path, monkeypatch, s3):
    """The command pointed at a data dir and the moto bucket. No database yet: the command must never need one."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KYAPEHNU_CONFIG_FILE", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("KYAPEHNU_FILES_BUCKET", TEST_BUCKET)
    get_settings.cache_clear()
    yield SimpleNamespace(data_dir=data_dir, local=LocalStore(data_dir), s3=s3)
    get_settings.cache_clear()


def run(argv: list[str], capsys, *, code: int | None = None) -> tuple[str, str]:
    """Run `kyapehnu copy-pictures ARGV`; with `code` it must exit with it. Returns (stdout, stderr)."""
    capsys.readouterr()
    if code is None:
        main(["copy-pictures", *argv])
    else:
        with pytest.raises(SystemExit) as stop:
            main(["copy-pictures", *argv])
        assert stop.value.code == code
    out = capsys.readouterr()
    return out.out, out.err


def line(copied=0, present=0, failed=0, deleted=0, to="s3") -> str:
    return f"pictures_copied copied={copied} present={present} failed={failed} deleted={deleted} direction={to}\n"


def local_files(data_dir: Path) -> list[str]:
    return sorted(p.relative_to(data_dir).as_posix() for p in data_dir.rglob("*") if p.is_file())


def test_to_s3_copies_the_disk_pictures_into_the_bucket_and_a_second_run_finds_them_present(stack, capsys):
    plant(stack.local)

    first = run(["--to", "s3"], capsys)
    second = run(["--to", "s3"], capsys)

    assert first == (line(copied=N), "")
    assert second == (line(present=N), "")
    assert bucket_keys(stack.s3) == sorted(KEYS)
    for key in KEYS:
        assert stack.s3.get_object(Bucket=TEST_BUCKET, Key=key)["Body"].read() == data_for(key)
    assert local_files(stack.data_dir) == sorted(KEYS)


def test_to_local_brings_the_bucket_pictures_back_and_skips_the_identical_ones(stack, capsys):
    plant(stack.local, KEYS[:2])  # two are already on the disk, identical
    for key in KEYS:
        stack.s3.put_object(Bucket=TEST_BUCKET, Key=key, Body=data_for(key))

    out = run(["--to", "local"], capsys)

    assert out == (line(copied=N - 2, present=2, to="local"), "")
    assert local_files(stack.data_dir) == sorted(KEYS)
    for key in KEYS:
        assert stack.local.read_bytes(key) == data_for(key)
    assert bucket_keys(stack.s3) == sorted(KEYS)  # nothing is taken from the bucket


def test_the_command_never_opens_the_database_and_leaves_an_existing_one_byte_for_byte(stack, capsys):
    plant(stack.local)
    run(["--to", "s3"], capsys)
    assert not (stack.data_dir / "kyapehnu.db").exists()  # a stack with no database yet gets none from this command

    engine = prepare_database(stack.data_dir)
    with Session(engine) as s:
        upsert_google_user(s, sub="sub-x", email="x@example.com", name="x", now=utcnow())
    engine.dispose()
    database = stack.data_dir / "kyapehnu.db"
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    run(["--to", "s3"], capsys)
    run(["--to", "local"], capsys)

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_an_unreadable_disk_picture_gives_one_failure_exit_1_and_deletes_no_local_file(stack, capsys, monkeypatch):
    plant(stack.local)
    unreadable, real_open = KEYS[4], LocalStore.open

    def open_(self, key):
        if key == unreadable:
            raise PermissionError("not for this process")
        return real_open(self, key)

    monkeypatch.setattr(LocalStore, "open", open_)

    out = run(["--to", "s3"], capsys, code=FAILED)

    assert out == (line(copied=N - 1, failed=1), "")
    assert bucket_keys(stack.s3) == sorted(set(KEYS) - {unreadable})
    assert local_files(stack.data_dir) == sorted(KEYS)


def test_a_bucket_object_with_other_bytes_is_failed_exit_1_and_stays_as_it_was(stack, capsys):
    plant(stack.local)
    stack.s3.put_object(Bucket=TEST_BUCKET, Key=KEYS[0], Body=b"somebody else's bytes")

    out = run(["--to", "s3"], capsys, code=FAILED)

    assert out == (line(copied=N - 1, failed=1), "")
    assert stack.s3.get_object(Bucket=TEST_BUCKET, Key=KEYS[0])["Body"].read() == b"somebody else's bytes"


def test_a_store_that_goes_down_half_way_counts_the_rest_as_failed_and_deletes_only_what_was_verified(
    stack, capsys, monkeypatch
):
    plant(stack.local)
    stored = put_fails_after(monkeypatch, 2)

    out = run(["--to", "s3", "--delete-source"], capsys, code=FAILED)

    assert out == (line(copied=2, failed=N - 2, deleted=2), "")
    assert bucket_keys(stack.s3) == sorted(stored)
    assert local_files(stack.data_dir) == sorted(set(KEYS) - set(stored))  # what is not in the bucket is still here


def test_delete_source_to_s3_leaves_no_picture_file_on_the_disk(stack, capsys):
    plant(stack.local)

    out = run(["--to", "s3", "--delete-source"], capsys)

    assert out == (line(copied=N, deleted=N), "")
    assert bucket_keys(stack.s3) == sorted(KEYS)
    assert local_files(stack.data_dir) == []  # the AC-34 `find /data/images -type f | wc -l` is 0


@pytest.mark.parametrize("to", ["s3", "local"])
def test_without_a_bucket_setting_the_command_refuses_and_changes_nothing(stack, capsys, monkeypatch, to):
    plant(stack.local)
    monkeypatch.delenv("KYAPEHNU_FILES_BUCKET")
    get_settings.cache_clear()

    out, err = run(["--to", to], capsys, code=REFUSED)

    assert out == ""
    assert err == "KYAPEHNU_FILES_BUCKET is not set: copy-pictures needs the bucket. Nothing was changed.\n"
    assert local_files(stack.data_dir) == sorted(KEYS)
    assert bucket_keys(stack.s3) == []


def test_delete_source_with_to_local_is_refused_and_changes_nothing(stack, capsys):
    plant(stack.local, KEYS[:2])
    for key in KEYS:
        stack.s3.put_object(Bucket=TEST_BUCKET, Key=key, Body=data_for(key))

    out, err = run(["--to", "local", "--delete-source"], capsys, code=REFUSED)

    assert out == ""
    assert "--delete-source" in err
    assert local_files(stack.data_dir) == sorted(KEYS[:2])  # nothing was copied
    assert bucket_keys(stack.s3) == sorted(KEYS)  # nothing was deleted
