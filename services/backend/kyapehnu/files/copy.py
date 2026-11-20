"""Copy every picture of one store into another (`kyapehnu copy-pictures`, ADR-6).

A key already in the target with the same bytes counts as present, with other bytes as failed (never overwritten).
A source is deleted only after the target's checksum was read again."""

import logging
from dataclasses import dataclass

from kyapehnu.files.errors import MissingFileError
from kyapehnu.files.keys import ROOT
from kyapehnu.files.store import FileStore

log = logging.getLogger(__name__)


@dataclass
class CopyCounts:
    copied: int = 0
    present: int = 0
    failed: int = 0
    deleted: int = 0


def copy_all(source: FileStore, target: FileStore, *, delete_source: bool = False) -> CopyCounts:
    """One key never stops the run: any error on it is that key's failure, logged by class only."""
    counts = CopyCounts()
    for stored in source.list(ROOT):
        try:
            _copy_one(stored.key, source, target, delete_source, counts)
        except Exception as e:  # a bad picture, a lost connection or a refused write: the others still move
            counts.failed += 1
            log.warning("event=copy_failed error=%s", type(e).__name__)
    return counts


def _copy_one(key: str, source: FileStore, target: FileStore, delete_source: bool, counts: CopyCounts) -> None:
    wanted = source.checksum(key)
    try:
        found: str | None = target.checksum(key)
    except MissingFileError:
        found = None
    copied = found is None
    if copied:
        with source.open(key) as f:
            target.put(key, f)
    elif found != wanted:
        counts.failed += 1
        return
    if delete_source:
        if target.checksum(key) != wanted:  # a key counts once: as failed here, never also as copied
            counts.failed += 1
            return
        source.delete(key)
        counts.deleted += 1
    if copied:
        counts.copied += 1
    else:
        counts.present += 1
