import logging
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

from kyapehnu.core.db import DB_FILE_NAME, check_revision, prepare_database
from kyapehnu.core.errors import ImportRefusedError
from kyapehnu.files import public as files

log = logging.getLogger(__name__)

STALE_DB_SUFFIXES: Final = ("-wal", "-shm")
ZIP_EPOCH: Final = (1980, 1, 1, 0, 0, 0)  # the earliest date a zip entry can carry


def backup(data_dir: Path, store: files.FileStore) -> Path:
    """Zip a consistent DB snapshot plus every picture of the store into data/backups/kyapehnu-YYYYmmdd-HHMMSS.zip.

    The archive is written under a name no listing or restore takes for a backup and renamed when it is complete, so
    a failure never leaves a truncated archive that looks like a good one."""
    out_dir = data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    partial = out_dir / f".kyapehnu-{uuid4().hex}.partial"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / DB_FILE_NAME
            _snapshot_db(data_dir / DB_FILE_NAME, snapshot)
            with zipfile.ZipFile(partial, "w") as z:
                z.write(snapshot, DB_FILE_NAME, compress_type=zipfile.ZIP_DEFLATED)
                # `list` yields only picture keys, so a stray object cannot make the backup unrestorable.
                for stored in sorted(store.list(files.ROOT), key=lambda f: f.key):
                    _add_picture(z, store, stored)
        out = _publish(partial, out_dir, f"kyapehnu-{datetime.now():%Y%m%d-%H%M%S}")
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return out


def _add_picture(z: zipfile.ZipFile, store: files.FileStore, stored: files.StoredFile) -> None:
    modified = max(time.localtime(stored.modified)[:6], ZIP_EPOCH)
    try:
        source = store.open(stored.key)
    except files.MissingFileError:  # deleted since the listing; the database snapshot was taken before the delete
        log.info("event=backup_picture_skipped reason=deleted_meanwhile")
        return
    with source as src, z.open(zipfile.ZipInfo(stored.key, date_time=modified), "w") as dst:
        shutil.copyfileobj(src, dst)  # ZipInfo stores uncompressed: pictures are already compressed


def _publish(partial: Path, out_dir: Path, stem: str) -> Path:
    """Give the finished archive a free name. `os.link` fails on a taken name, so two backups that finish in the same
    second cannot overwrite each other (and a failed one cannot delete the good one)."""
    n = 0
    while True:
        out = out_dir / (f"{stem}.zip" if n == 0 else f"{stem}-{n}.zip")
        try:
            os.link(partial, out)
        except FileExistsError:
            n += 1
        else:
            partial.unlink()
            return out


def _snapshot_db(source: Path, target: Path) -> None:
    """The sqlite backup API reads through the WAL, so committed rows not yet checkpointed are included."""
    # Read-only, so a missing database is an error instead of a new empty file.
    src, dst = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def restore(data_dir: Path, store: files.FileStore, archive: Path) -> None:
    """Put a backup into a stack without accounts. Refuses before it writes anything unless every member of the archive
    is the database or a picture key. The pictures go into the store first and the database last, by `os.replace` from
    a staging folder on the same volume, so a failure leaves no database that a second run would refuse as "in use"
    and removes the pictures this run wrote."""
    try:
        z = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as e:
        raise ImportRefusedError("The archive cannot be read as a zip file. Nothing was changed.") from e
    with z:
        names = z.namelist()
        _check_archive_names(names)
        _check_target(data_dir / DB_FILE_NAME)
        data_dir.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=data_dir) as staging:
            staged = Path(staging) / DB_FILE_NAME
            try:
                with z.open(DB_FILE_NAME) as member, staged.open("wb") as out:
                    shutil.copyfileobj(member, out)
                _check_archived_database(staged)
                _put_pictures(z, names, store, written)
                for suffix in STALE_DB_SUFFIXES:
                    (data_dir / f"{DB_FILE_NAME}{suffix}").unlink(missing_ok=True)  # never replay an old log
                os.replace(staged, data_dir / DB_FILE_NAME)
            except BaseException:
                files.discard(store, written)
                raise
    prepare_database(data_dir).dispose()


def _check_archive_names(names: list[str]) -> None:
    if DB_FILE_NAME not in names:
        raise ImportRefusedError("The archive holds no database (kyapehnu.db). Nothing was changed.")
    if any(n != DB_FILE_NAME and not files.KEY_PATTERN.fullmatch(n) for n in names):
        raise ImportRefusedError(
            "The archive holds a file that a Kya Pehnu backup never contains. Nothing was changed."
        )


def _read_only(db: Path, statement: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as con:
        return con.execute(statement).fetchall()


def _check_target(db: Path) -> None:
    """A Phase 3 or unknown database is refused by `check_revision`; one with accounts is somebody's stack."""
    if not db.exists():
        return
    check_revision(db)
    has_user_table = _read_only(db, "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user'")
    if has_user_table and _read_only(db, 'SELECT 1 FROM "user" LIMIT 1'):
        raise ImportRefusedError("This stack already has accounts: restore into an empty one. Nothing was changed.")


def _check_archived_database(staged: Path) -> None:
    check_revision(staged)  # not SQLite, Phase 3, unknown revision
    try:
        stamped = _read_only(staged, "SELECT version_num FROM alembic_version")
    except sqlite3.DatabaseError:  # no such table: an empty database
        stamped = []
    if len(stamped) != 1:
        raise ImportRefusedError("The database in the archive has no revision stamp. Nothing was changed.")


def _put_pictures(z: zipfile.ZipFile, names: list[str], store: files.FileStore, written: list[str]) -> None:
    for name in names:
        if name == DB_FILE_NAME:
            continue
        if _already_stored(z, name, store):
            continue  # not this run's to undo: a failed restore must not delete what was there before
        with z.open(name) as member:
            store.put(name, member)  # all or nothing, so a key is listed once it is really there
        written.append(name)


def _already_stored(z: zipfile.ZipFile, name: str, store: files.FileStore) -> bool:
    try:
        stored = store.checksum(name)
    except files.MissingFileError:
        return False
    with z.open(name) as member:
        return stored == files.sha256_b64(files.read_chunks(member))
