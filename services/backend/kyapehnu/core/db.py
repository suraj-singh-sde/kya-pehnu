import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry
from sqlmodel import Session, SQLModel, create_engine

from kyapehnu.core.errors import NotFoundError, PreOwnershipDatabaseError, StartupRefusedError, UnknownDatabaseError

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
DB_FILE_NAME: Final = "kyapehnu.db"
BUSY_TIMEOUT_S: Final = 5
# The Alembic chain of Kya Pehnu Phase 3: those databases have no owner column and are imported, never upgraded.
PHASE3_REVISIONS: Final = frozenset(
    {"971397b5ec4d", "b2c4e6a81f30", "c7e2a9d40f13", "d4f1a7c90b22", "e5b8c2d17a40", "f6c9d3e28b51", "a7d2e4f39c60"}
)
OWNERSHIP_REVISION: Final = "b8e3f5a40d71"
HEAD_REVISION: Final = "a9c1d4e7f201"  # the item description


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_engine(data_dir: Path) -> Engine:
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{(data_dir / DB_FILE_NAME).as_posix()}",
        connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_S},
        hide_parameters=True,  # a logged DB error must not carry the user's text (wear reasons, event notes)
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


def migrate(engine: Engine, revision: str = "head") -> None:
    """Upgrade the database to `revision`, by default the latest (migrations ship inside the package)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


def _refusal(error: StartupRefusedError, reason: str) -> StartupRefusedError:
    log.error("event=startup_refused reason=%s", reason)
    return error


def _stamped_revisions(db_path: Path) -> list[str] | None:
    """The `alembic_version` rows of an existing database, or None when it holds no tables. Opens read-only."""
    con = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)  # as_uri needs an absolute path
    try:
        tables = {
            name
            for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not tables:
            return None
        if "alembic_version" not in tables:
            return []
        return [version for (version,) in con.execute("SELECT version_num FROM alembic_version")]
    finally:
        con.close()


def check_revision(db_path: Path) -> None:
    """Refuse a database this version must not touch (Phase 3, unknown, unreadable). Never writes to it.

    A missing or empty file is fine: it is created at head by `migrate`."""
    if not db_path.exists():
        return
    try:
        stamped = _stamped_revisions(db_path)
    except sqlite3.DatabaseError as e:
        raise _refusal(
            UnknownDatabaseError("Not a Kya Pehnu database, or unreadable. Nothing was changed."), "unreadable_db"
        ) from e
    if stamped is None:
        return
    known = {script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()}
    if len(stamped) == 1 and stamped[0] in PHASE3_REVISIONS:
        raise _refusal(
            PreOwnershipDatabaseError(
                "This database is from Kya Pehnu Phase 3. Import it with `kyapehnu import-phase3` (README, Migrating). "
                "Nothing was changed."
            ),
            "pre_ownership_db",
        )
    if len(stamped) != 1 or stamped[0] not in known:
        raise _refusal(UnknownDatabaseError("Unknown database revision. Nothing was changed."), "unknown_db")


def prepare_database(data_dir: Path) -> Engine:
    """The engine of an up-to-date database. Raises `StartupRefusedError` before anything is written when the file
    already there is not a database of this version."""
    check_revision(data_dir / DB_FILE_NAME)
    engine = make_engine(data_dir)
    migrate(engine)
    return engine


def get_one[M: SQLModel](s: Session, model: type[M], ident: int) -> M:
    row = s.get(model, ident)
    if row is None:
        raise NotFoundError(f"{model.__name__} {ident} not found")
    return row


def get_owned[M: SQLModel](user_id: int, s: Session, model: type[M], ident: int) -> M:
    """A row of `model` that belongs to `user_id`. Another user's row looks exactly like a missing one (FR-11)."""
    row = s.get(model, ident)
    if row is None or getattr(row, "user_id", None) != user_id:
        raise NotFoundError(f"{model.__name__} {ident} not found")
    return row
