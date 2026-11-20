"""The picture store contract: the one interface the disk and S3 adapters implement."""

import base64
import hashlib
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import IO, Final, Protocol

from starlette.requests import Request
from starlette.responses import Response

HASH_CHUNK_BYTES: Final = 1024 * 1024


@dataclass(frozen=True)
class StoredFile:
    key: str
    modified: float  # epoch seconds: S3 LastModified, local st_mtime


class FileStore(Protocol):
    def put(self, key: str, body: bytes | IO[bytes]) -> None:
        """Store the bytes (or the rest of the stream) at `key`, replacing what was there. All or nothing."""
        ...

    def open(self, key: str) -> AbstractContextManager[IO[bytes]]:
        """A seekable read handle (for Pillow, zip, copy). MissingFileError when there is no such picture."""
        ...

    def read_bytes(self, key: str) -> bytes:
        """The whole picture in memory: for cutouts and thumbs, not originals."""
        ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None:
        """A missing key is not an error."""
        ...

    def list(self, prefix: str) -> Iterator[StoredFile]:
        """The picture keys that start with `prefix`. Anything that is not a picture key is not ours and is skipped."""
        ...

    def checksum(self, key: str) -> str:
        """Base64 SHA-256 of the bytes."""
        ...

    def serve(self, key: str, request: Request) -> Response:
        """200, 206, 304 or 416 for GET and HEAD, with the validators. No Cache-Control: the route adds it."""
        ...

    def ping(self) -> None:
        """StoreUnavailableError when the store is not usable."""
        ...


def read_chunks(stream: IO[bytes], size: int = HASH_CHUNK_BYTES) -> Iterator[bytes]:
    return iter(lambda: stream.read(size), b"")


def sha256_b64(chunks: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")
