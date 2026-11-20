"""The disk store: pictures under <data dir>/images/<user id>/<kind>/<name>."""

import os
import shutil
from collections.abc import Iterator
from contextlib import AbstractContextManager, suppress
from pathlib import Path
from stat import S_ISREG
from typing import IO
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from kyapehnu.files.errors import MissingFileError, StoreUnavailableError
from kyapehnu.files.keys import KEY_PATTERN, ROOT, check
from kyapehnu.files.store import StoredFile, read_chunks, sha256_b64

# FileResponse (Range, ETag) plus the conditional-request answer; it is only asked to build the response.
_static = StaticFiles(check_dir=False)


def _scan(folder: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(folder) as entries:
            return sorted(entries, key=lambda entry: entry.name)
    except (FileNotFoundError, NotADirectoryError):
        return []


def _overlaps(folder_prefix: str, prefix: str) -> bool:
    return folder_prefix.startswith(prefix) or prefix.startswith(folder_prefix)


def _subfolders(folder: Path, folder_key: str, prefix: str) -> list[os.DirEntry[str]]:
    """The real (never linked) folders of `folder` that can hold a key starting with `prefix`."""
    return [
        entry
        for entry in _scan(folder)
        if entry.is_dir(follow_symlinks=False) and _overlaps(f"{folder_key}{entry.name}/", prefix)
    ]


def _prune_empty_folders(path: Path) -> None:
    # ponytail: a concurrent put into the same folder can lose its folder here and then fails (retryable); a lock per
    # owner and kind if that ever shows up.
    for folder in (path.parent, path.parent.parent):  # the kind folder, then the user folder, while empty
        with suppress(OSError):
            folder.rmdir()


class LocalStore:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._resolved_data_dir = data_dir.resolve()
        self._images = data_dir / ROOT.rstrip("/")
        self._images.mkdir(parents=True, exist_ok=True)

    def _stat_plain_file(self, key: str) -> tuple[Path, os.stat_result]:
        """The path and stat of a regular file at `key`. A link anywhere on the way (which could lead into another
        user's folder or out of the data dir), a folder or a missing file is MissingFileError."""
        path = self._data_dir / check(key)
        try:
            if path.resolve() != self._resolved_data_dir / key:
                raise MissingFileError
            stat = path.stat()
        except (OSError, RuntimeError) as e:  # missing, unreadable, or a link loop
            raise MissingFileError from e
        if not S_ISREG(stat.st_mode):
            raise MissingFileError
        return path, stat

    def _unlinked_path(self, key: str) -> Path | None:
        """The path for a write or delete at `key`, or None when a user or kind folder on the way is a link
        (the rule `_stat_plain_file` applies to reads): a write must not land in another user's folder."""
        path = self._data_dir / check(key)
        if path.parent.resolve() != self._resolved_data_dir / key.rpartition("/")[0]:
            return None
        return path

    def put(self, key: str, body: bytes | IO[bytes]) -> None:
        path = self._unlinked_path(key)
        if path is None:
            raise StoreUnavailableError
        path.parent.mkdir(parents=True, exist_ok=True)
        # A dot-name no listing shows, in the same folder so the rename cannot cross file systems.
        tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with tmp.open("wb") as out:
                if isinstance(body, bytes):
                    out.write(body)
                else:
                    shutil.copyfileobj(body, out)
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            _prune_empty_folders(path)  # a failed first write leaves no folder of its own behind
            raise

    def open(self, key: str) -> AbstractContextManager[IO[bytes]]:
        path, _ = self._stat_plain_file(key)
        try:
            return path.open("rb")
        except FileNotFoundError as e:  # deleted since the stat
            raise MissingFileError from e

    def read_bytes(self, key: str) -> bytes:
        with self.open(key) as f:
            return f.read()

    def exists(self, key: str) -> bool:
        try:
            self._stat_plain_file(key)
        except MissingFileError:
            return False
        return True

    def delete(self, key: str) -> None:
        path = self._unlinked_path(key)
        if path is None:
            return
        path.unlink(missing_ok=True)
        _prune_empty_folders(path)

    def list(self, prefix: str) -> Iterator[StoredFile]:
        for user in _subfolders(self._images, ROOT, prefix):
            for kind in _subfolders(Path(user.path), f"{ROOT}{user.name}/", prefix):
                folder_key = f"{ROOT}{user.name}/{kind.name}/"
                for entry in _scan(Path(kind.path)):
                    key = folder_key + entry.name
                    if not (
                        key.startswith(prefix) and KEY_PATTERN.fullmatch(key) and entry.is_file(follow_symlinks=False)
                    ):
                        continue
                    try:
                        modified = entry.stat(follow_symlinks=False).st_mtime
                    except FileNotFoundError:  # deleted while listing
                        continue
                    yield StoredFile(key, modified)

    def checksum(self, key: str) -> str:
        with self.open(key) as f:
            return sha256_b64(read_chunks(f))

    def serve(self, key: str, request: Request) -> Response:
        path, stat = self._stat_plain_file(key)
        return _static.file_response(path, stat, request.scope)

    def ping(self) -> None:
        if not self._images.is_dir():
            raise StoreUnavailableError
