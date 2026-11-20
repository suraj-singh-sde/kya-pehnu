"""Import of a Phase 3 data directory into one account (LLD 3.7).

The source is only read: its database is copied to a scratch folder and upgraded there, its images are read as files.
Everything the stack gains is written in this order: the pictures into the store, then every row in one transaction. A
failure removes the pictures and rolls the rows back."""

import logging
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlmodel import Session

from kyapehnu.auth.public import user_id_for_email
from kyapehnu.core.config import Settings
from kyapehnu.core.db import BUSY_TIMEOUT_S, DB_FILE_NAME, PHASE3_REVISIONS, make_engine, migrate, prepare_database
from kyapehnu.core.errors import ImportRefusedError
from kyapehnu.files import public as files

log = logging.getLogger(__name__)

LAST_PHASE3_REVISION: Final = "a7d2e4f39c60"
SOURCE_IMAGE: Final = re.compile(rf"images/(?:{files.KIND_ALTERNATIVES})/{files.IMAGE_NAME}")
WAL_SUFFIX: Final = "-wal"
# (table, key of the `import_done` line), in the order of the line (LLD 3.6)
COUNTED: Final = (
    ("item", "items"),
    ("photo", "photos"),
    ("ingestjob", "jobs"),
    ("ingestpiece", "pieces"),
    ("itemcorrection", "corrections"),
    ("event", "events"),
    ("outfit", "outfits"),
    ("wearlog", "wears"),
    ("chatsession", "chats"),
    ("chatmessage", "messages"),
    ("toolcall", "tool_calls"),
    ("llmcall", "llm_calls"),
    ("feedback", "feedback"),
)
# parents before children: the stack enforces its foreign keys
INSERT_ORDER: Final = (
    "ingestjob",
    "photo",
    "item",
    "itemcorrection",
    "ingestpiece",
    "llmcall",
    "event",
    "outfit",
    "wearlog",
    "chatsession",
    "chatmessage",
    "toolcall",
    "feedback",
)
PATH_COLUMNS: Final = {
    "photo": ("original_path", "normalized_path"),
    "item": ("cutout_path", "thumb_path", "alt_cutout_path", "alt_thumb_path"),
    "ingestpiece": ("cutout_path", "thumb_path", "alt_cutout_path", "alt_thumb_path"),
    "outfit": ("collage_path",),
}
ITEM_LIST_TABLES: Final = ("outfit", "wearlog")  # the tables whose `item_ids` column holds a JSON list of item ids
IMAGES_PREFIX_LENGTH: Final = len("images/")
NOT_KYAPEHNU: Final = "The source is not a Kya Pehnu Phase 3 database. Nothing was changed."
UNCHANGED: Final = "Nothing was changed."
IN_USE: Final = "Refused: already imported or account in use. Nothing was changed."


@dataclass(frozen=True)
class ImportCounts:
    user_id: int
    rows: dict[str, int]  # key of the done line -> rows imported
    images: int
    missing_images: int

    def line(self) -> str:
        """The one line the CLI prints. It names the account by id, never by email (AC-49)."""
        pairs = {"user_id": self.user_id, **self.rows, "images": self.images, "missing_images": self.missing_images}
        return "import_done " + " ".join(f"{key}={value}" for key, value in pairs.items())


def run(settings: Settings, store: files.FileStore, source: Path, email: str) -> ImportCounts:
    engine = prepare_database(settings.data_dir)
    try:
        with Session(engine) as s:
            user_id = user_id_for_email(s, email)
    except LookupError as e:
        raise ImportRefusedError("No single account matches that email. Nothing was changed.") from e
    finally:
        engine.dispose()
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / DB_FILE_NAME
        _copy_database(source, copy)
        _require_phase3(copy)
        engine = make_engine(copy.parent)
        try:
            migrate(engine, LAST_PHASE3_REVISION)
        finally:
            engine.dispose()
        counts = _import(settings.data_dir, store, source, copy, user_id)
    log.info("event=%s", counts.line())
    return counts


def _copy_database(source: Path, copy: Path) -> None:
    """Copy the database file, and its write-ahead log when the old install did not shut down cleanly: SQLite needs a
    writable folder to read a log, which a read-only mount is not, and the log may hold committed rows."""
    db = source / DB_FILE_NAME
    if not db.is_file():
        raise ImportRefusedError("The source folder holds no kyapehnu.db. Nothing was changed.")
    shutil.copyfile(db, copy)
    wal = db.with_name(db.name + WAL_SUFFIX)
    if wal.is_file():
        shutil.copyfile(wal, copy.with_name(copy.name + WAL_SUFFIX))


def _require_phase3(copy: Path) -> None:
    try:
        with closing(sqlite3.connect(copy)) as con:
            stamped = con.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.DatabaseError as e:  # not SQLite, or no revision table
        raise ImportRefusedError(NOT_KYAPEHNU) from e
    if len(stamped) != 1 or stamped[0][0] not in PHASE3_REVISIONS:
        raise ImportRefusedError("The source database is not from Kya Pehnu Phase 3. Nothing was changed.")


def _import(data_dir: Path, store: files.FileStore, source: Path, copy: Path, user_id: int) -> ImportCounts:
    written: list[str] = []
    with closing(sqlite3.connect(data_dir / DB_FILE_NAME, timeout=BUSY_TIMEOUT_S, isolation_level=None)) as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA trusted_schema = OFF")  # nothing in a crafted database may run as a function
        con.execute("ATTACH DATABASE ? AS src", (str(copy),))
        referenced = _checked_paths(con)
        _preflight(con, user_id)
        _require_closed_references(con)
        try:
            copied = _copy_images(source, store, user_id, referenced, written)
            rows = _write_rows(con, user_id)
        except BaseException:
            log.error("event=import_failed user_id=%d", user_id)
            files.discard(store, written)
            raise
    return ImportCounts(user_id, rows, images=len(written), missing_images=len(referenced - copied))


def _checked_paths(con: sqlite3.Connection) -> set[str]:
    """Every image path the source rows name. One that is not `images/<kind>/<name>` makes the whole database a
    crafted one (it could point outside the folder it is copied to)."""
    found: set[str] = set()
    for table, columns in PATH_COLUMNS.items():
        for column in columns:
            for (value,) in con.execute(f'SELECT DISTINCT "{column}" FROM src."{table}" WHERE "{column}" IS NOT NULL'):
                if not isinstance(value, str) or not SOURCE_IMAGE.fullmatch(value):
                    raise ImportRefusedError(
                        f"{table}.{column} of the source holds a path no image has. Nothing was changed."
                    )
                found.add(value)
    return found


def _preflight(con: sqlite3.Connection, user_id: int) -> None:
    """Refuse before anything is written (and again inside the transaction)."""
    for table, _key in COUNTED:
        if con.execute(f'SELECT 1 FROM main."{table}" WHERE user_id = ? LIMIT 1', (user_id,)).fetchone():
            raise ImportRefusedError(IN_USE)
    for table in INSERT_ORDER:
        if con.execute(f'SELECT 1 FROM src."{table}" AS s JOIN main."{table}" AS m ON m.id = s.id LIMIT 1').fetchone():
            raise ImportRefusedError(
                f"An id in {table} of the source is already taken on this stack. Nothing was changed."
            )
    # Ids are never reused (ADR 0009, 0010): the counters of the stack must be below every id the source brings.
    if con.execute(
        "SELECT 1 FROM src.item WHERE id <= (SELECT coalesce(max(last_id), 0) FROM main.itemseq) LIMIT 1"
    ).fetchone():
        raise ImportRefusedError(
            "The item counter of this stack is past an item id of the source. Nothing was changed."
        )
    if con.execute(
        "SELECT 1 FROM src.chatsession WHERE id <= "
        "(SELECT coalesce(max(seq), 0) FROM main.sqlite_sequence WHERE name = 'chatsession') LIMIT 1"
    ).fetchone():
        raise ImportRefusedError("A chat id of the source was used on this stack before. Nothing was changed.")


def _require_closed_references(con: sqlite3.Connection) -> None:
    """Refuse a source whose rows point at a row that is not in the source. The stack enforces its foreign keys, but
    a row of the source that names an id of another account's row would be accepted, and A's data would then hold a
    link into B's. The names come from the stack's own schema, never from the source."""
    for table in INSERT_ORDER:
        for _, _, parent, column, target, *_ in con.execute(f'PRAGMA main.foreign_key_list("{table}")').fetchall():
            if parent == "user":
                continue
            dangling = con.execute(
                f'SELECT 1 FROM src."{table}" WHERE "{column}" IS NOT NULL'
                f' AND "{column}" NOT IN (SELECT "{target}" FROM src."{parent}") LIMIT 1'
            ).fetchone()
            if dangling:
                raise ImportRefusedError(f"{table}.{column} of the source names a row that is not in it. {UNCHANGED}")
    for table in ITEM_LIST_TABLES:
        try:
            # An id that is not an item of the source is fine (an item deleted since) unless the stack holds it.
            foreign = con.execute(
                f'SELECT 1 FROM src."{table}" AS s, json_each(s.item_ids) AS j'
                " WHERE j.value NOT IN (SELECT id FROM src.item) AND j.value IN (SELECT id FROM main.item) LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError as e:  # malformed JSON
            raise ImportRefusedError(f"{table}.item_ids of the source is not a list. {UNCHANGED}") from e
        if foreign:
            raise ImportRefusedError(f"{table}.item_ids of the source names an item that is not in it. {UNCHANGED}")


def _copy_images(
    source: Path, store: files.FileStore, user_id: int, referenced: set[str], written: list[str]
) -> set[str]:
    """Put the files the rows name and every mannequin picture (their names are item ids, no row names them) into the
    store at images/<user_id>/<kind>/<name>. Returns the referenced paths that were copied."""
    # Only the source folder itself may be a link: resolving `images` too would let a link make any folder the root.
    root = source.resolve() / "images"
    mannequins = (
        {f"images/mannequin/{p.name}" for p in (root / "mannequin").iterdir() if re.fullmatch(files.IMAGE_NAME, p.name)}
        if (root / "mannequin").is_dir()
        else set()
    )
    copied: set[str] = set()
    for rel in sorted(referenced | mannequins):
        file = root / rel[IMAGES_PREFIX_LENGTH:]
        # A link at any level (images, the kind folder, the file) would put a file of the host into the folder the
        # person is served from, so the resolved path must be the plain one.
        if not file.is_file() or file.resolve() != file:
            continue
        kind, name = rel[IMAGES_PREFIX_LENGTH:].split("/")
        picture = files.key(user_id, kind, name)
        with file.open("rb") as body:
            store.put(picture, body)  # all or nothing, so a key is listed once it is really there
        written.append(picture)
        copied.add(rel)
    return copied


def _columns(con: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in con.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _shared_columns(con: sqlite3.Connection, table: str, *, skip: tuple[str, ...]) -> list[str]:
    """The columns the source and the stack both have. Names come from the stack's own schema, never from the source
    text, so they are safe to put in a statement."""
    source_columns = _columns(con, "src", table)
    if not source_columns:
        raise ImportRefusedError(f"The source has no {table} table. Nothing was changed.")
    stack_columns = set(_columns(con, "main", table))
    return [c for c in source_columns if c in stack_columns and c not in skip]


def _write_rows(con: sqlite3.Connection, user_id: int) -> dict[str, int]:
    con.execute("BEGIN IMMEDIATE")
    try:
        _preflight(con, user_id)
        inserted = {table: _insert_table(con, table, user_id) for table in INSERT_ORDER}
        _copy_profile(con, user_id)
        con.execute(
            "UPDATE main.itemseq SET last_id = max(last_id, coalesce((SELECT max(last_id) FROM src.itemseq), 0),"
            " coalesce((SELECT max(id) FROM src.item), 0)) WHERE id = 1"
        )
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return {key: inserted[table] for table, key in COUNTED}


def _insert_table(con: sqlite3.Connection, table: str, user_id: int) -> int:
    columns = _shared_columns(con, table, skip=("user_id",))
    # `images/<kind>/<name>` becomes `images/<user_id>/<kind>/<name>`; a NULL stays NULL
    values = [
        f"'images/' || :user_id || '/' || substr(\"{c}\", {IMAGES_PREFIX_LENGTH + 1})"
        if c in PATH_COLUMNS.get(table, ())
        else f'"{c}"'
        for c in columns
    ]
    names = ", ".join(f'"{c}"' for c in columns)
    return con.execute(
        f'INSERT INTO main."{table}" ({names}, user_id) SELECT {", ".join(values)}, :user_id FROM src."{table}"',
        {"user_id": user_id},
    ).rowcount


def _copy_profile(con: sqlite3.Connection, user_id: int) -> None:
    """The profile of the source (row 1) overwrites the profile every account is created with."""
    columns = _shared_columns(con, "userprofile", skip=("id", "user_id"))
    names = ", ".join(f'"{c}"' for c in columns)
    updates = ", ".join(f'"{c}" = excluded."{c}"' for c in columns)
    con.execute(
        f"INSERT INTO main.userprofile ({names}, user_id) SELECT {names}, :user_id FROM src.userprofile WHERE id = 1"
        f" ON CONFLICT (user_id) DO UPDATE SET {updates}",
        {"user_id": user_id},
    )
