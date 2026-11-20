"""What the other domains may use from the files domain (LLD 3.8)."""

import logging
from collections.abc import Iterable
from pathlib import Path

from kyapehnu.files.copy import CopyCounts, copy_all
from kyapehnu.files.errors import InvalidKeyError, MissingFileError, StoreUnavailableError
from kyapehnu.files.keys import IMAGE_NAME, KEY_PATTERN, KIND_ALTERNATIVES, KINDS, ROOT, key, parse_key, prefix
from kyapehnu.files.local import LocalStore
from kyapehnu.files.store import FileStore, StoredFile, read_chunks, sha256_b64

__all__ = [
    "IMAGE_NAME",
    "KEY_PATTERN",
    "KIND_ALTERNATIVES",
    "KINDS",
    "ROOT",
    "FileStore",
    "InvalidKeyError",
    "LocalStore",
    "MissingFileError",
    "StoreUnavailableError",
    "CopyCounts",
    "StoredFile",
    "build_store",
    "copy_all",
    "discard",
    "key",
    "parse_key",
    "prefix",
    "read_chunks",
    "sha256_b64",
]

log = logging.getLogger(__name__)


def build_store(data_dir: Path, bucket: str | None, region: str) -> FileStore:
    """The disk store, or the bucket's when one is set. A laptop with no bucket never imports boto3."""
    if bucket is None:
        return LocalStore(data_dir)
    from kyapehnu.files.s3 import S3Store

    return S3Store(bucket, region)


def discard(store: FileStore, keys: Iterable[str | None]) -> int:
    """Delete after a commit or to undo a write, best effort: a leftover is the orphan sweep's to remove later.

    Returns how many keys were deleted. A store that stops answering ends the run (one timeout, not one per key).
    """
    pending = [k for k in keys if k is not None]
    deleted = 0
    for n, key_to_delete in enumerate(pending):
        try:
            store.delete(key_to_delete)
        except StoreUnavailableError as e:
            log.warning("event=files_delete_failed error=%s left=%d", type(e).__name__, len(pending) - n)
            break
        except OSError as e:
            log.warning("event=files_delete_failed error=%s left=1", type(e).__name__)
        else:
            deleted += 1
    return deleted
