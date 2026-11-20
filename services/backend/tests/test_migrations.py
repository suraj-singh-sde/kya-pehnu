"""Startup rule and revision b8e3f5a40d71 (LLD 3.5, 5.1, 5.2; AC-22, AC-48): the schema is created at head in WAL mode,
a Phase 3 or unknown or unreadable database is refused without a single byte written, and the migrated schema is
exactly what the models describe."""

import hashlib
import logging
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel

import kyapehnu.agent.models  # noqa: F401  registers the tables on SQLModel.metadata
import kyapehnu.auth.models  # noqa: F401
import kyapehnu.user.models  # noqa: F401
import kyapehnu.wardrobe.models  # noqa: F401
from kyapehnu.core import db as core_db
from kyapehnu.core import errors as core_errors
from kyapehnu.core.db import (
    HEAD_REVISION,
    MIGRATIONS_DIR,
    OWNERSHIP_REVISION,
    PHASE3_REVISIONS,
    check_revision,
    make_engine,
    prepare_database,
)

PRE_OWNERSHIP_TEXT = (
    "This database is from Kya Pehnu Phase 3. Import it with `kyapehnu import-phase3` (README, Migrating). "
    "Nothing was changed."
)
UNKNOWN_REVISION_TEXT = "Unknown database revision. Nothing was changed."
UNREADABLE_TEXT = "Not a Kya Pehnu database, or unreadable. Nothing was changed."
LAST_PHASE3 = "a7d2e4f39c60"
OWNED_TABLES = [
    "userprofile",
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
]
COMPOSITE_INDEXES = {
    "ingestjob": ("ix_ingestjob_user_id_id", ["user_id", "id"]),
    "photo": ("ix_photo_user_id", ["user_id"]),
    "item": ("ix_item_user_id_status", ["user_id", "status"]),
    "itemcorrection": ("ix_itemcorrection_user_id", ["user_id"]),
    "ingestpiece": ("ix_ingestpiece_user_id_state", ["user_id", "state"]),
    "llmcall": ("ix_llmcall_user_id_id", ["user_id", "id"]),
    "event": ("ix_event_user_id_starts_at", ["user_id", "starts_at"]),
    "wearlog": ("ix_wearlog_user_id_worn_on", ["user_id", "worn_on"]),
    "chatsession": ("ix_chatsession_user_id_updated_at", ["user_id", "updated_at"]),
    "chatmessage": ("ix_chatmessage_user_id", ["user_id"]),
    "toolcall": ("ix_toolcall_user_id_id", ["user_id", "id"]),
    "feedback": ("ix_feedback_user_id", ["user_id"]),
}


def error_class(name):
    """The startup-refusal classes live in core.errors (LLD 4.1) or core.db (work plan): either place is fine."""
    return getattr(core_db, name, None) or getattr(core_errors, name)


def alembic_config(connection):
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    return cfg


def build_db(data_dir: Path, revision: str) -> Path:
    """A database file migrated exactly to `revision`, closed (no -wal left behind)."""
    engine = make_engine(data_dir)
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), revision)
    engine.dispose()
    return data_dir / "kyapehnu.db"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def raw(path: Path, *statements: str) -> None:
    con = sqlite3.connect(path)
    for statement in statements:
        con.execute(statement)
    con.commit()
    con.close()


# ---- the revision chain ----------------------------------------------------------------------------------------


def test_the_phase3_revisions_are_the_seven_of_the_old_chain():
    assert PHASE3_REVISIONS == {
        "971397b5ec4d",
        "b2c4e6a81f30",
        "c7e2a9d40f13",
        "d4f1a7c90b22",
        "e5b8c2d17a40",
        "f6c9d3e28b51",
        "a7d2e4f39c60",
    }
    assert OWNERSHIP_REVISION == "b8e3f5a40d71"


def test_the_head_follows_the_ownership_revision_which_follows_the_last_phase3_revision():
    scripts = ScriptDirectory(str(MIGRATIONS_DIR))

    assert scripts.get_current_head() == HEAD_REVISION
    assert scripts.get_revision(HEAD_REVISION).down_revision == "c1d2e3f4a5b6"  # avatar, almirah finish, layout
    assert scripts.get_revision("c1d2e3f4a5b6").down_revision == OWNERSHIP_REVISION
    assert scripts.get_revision(OWNERSHIP_REVISION).down_revision == LAST_PHASE3


def test_the_head_revision_adds_the_avatar_theme_and_layout_columns_and_can_be_undone(tmp_path):
    build_db(tmp_path, "head")
    engine = make_engine(tmp_path)

    def cols(table):
        return {c["name"] for c in inspect(engine).get_columns(table)}

    assert "avatar_url" in cols("user") and {"theme", "layout"} <= cols("userprofile")
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), OWNERSHIP_REVISION)
    assert "avatar_url" not in cols("user") and not ({"theme", "layout"} & cols("userprofile"))
    engine.dispose()


def test_the_startup_refusals_are_not_http_errors():
    startup = error_class("StartupRefusedError")

    assert issubclass(error_class("PreOwnershipDatabaseError"), startup)
    assert issubclass(error_class("UnknownDatabaseError"), startup)
    assert issubclass(startup, Exception) and not issubclass(startup, core_errors.AppError)


# ---- AC-22: a new or empty database goes to head ---------------------------------------------------------------


def test_a_missing_database_is_created_at_head_in_wal_mode(tmp_path):
    engine = prepare_database(tmp_path / "data")

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [HEAD_REVISION]
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    assert {"user", *OWNED_TABLES} <= set(inspect(engine).get_table_names())


@pytest.mark.parametrize("content", [b"", None], ids=["zero-byte file", "sqlite file without tables"])
def test_an_empty_database_file_goes_to_head(tmp_path, content):
    path = tmp_path / "kyapehnu.db"
    if content is None:
        raw(path, "PRAGMA user_version = 1")  # a valid SQLite header, no tables
    else:
        path.write_bytes(content)

    engine = prepare_database(tmp_path)

    assert "user" in inspect(engine).get_table_names()


def test_a_database_already_at_head_is_opened_with_its_data_intact(tmp_path):
    engine = prepare_database(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                'INSERT INTO "user" (google_sub, email, display_name, created_at, last_sign_in_at) '
                "VALUES ('g', 'a@example.com', 'A', '2026-01-01', '2026-01-01')"
            )
        )
    engine.dispose()

    again = prepare_database(tmp_path)

    with again.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM "user"')).scalar() == 1


def test_check_revision_never_creates_a_missing_file(tmp_path):
    check_revision(tmp_path / "kyapehnu.db")

    assert list(tmp_path.iterdir()) == []


def test_check_revision_leaves_a_database_at_head_byte_identical(tmp_path):
    path = build_db(tmp_path, "head")
    before = sha(path)

    check_revision(path)

    assert sha(path) == before


# ---- AC-48: refusals write nothing -----------------------------------------------------------------------------


@pytest.mark.parametrize("revision", sorted(PHASE3_REVISIONS))
def test_a_phase3_database_is_refused_unchanged(tmp_path, revision, caplog):
    caplog.set_level(logging.INFO)
    path = build_db(tmp_path, revision)
    before = sha(path)

    with pytest.raises(error_class("PreOwnershipDatabaseError")) as raised:
        prepare_database(tmp_path)

    assert str(raised.value) == PRE_OWNERSHIP_TEXT
    assert sha(path) == before
    assert "event=startup_refused reason=pre_ownership_db" in caplog.text


@pytest.mark.parametrize("revision", sorted(PHASE3_REVISIONS))
def test_check_revision_refuses_a_phase3_database_unchanged(tmp_path, revision):
    path = build_db(tmp_path, revision)
    before = sha(path)

    with pytest.raises(error_class("PreOwnershipDatabaseError")):
        check_revision(path)

    assert sha(path) == before


def test_an_unknown_revision_is_refused_unchanged(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    path = build_db(tmp_path, "head")
    raw(path, "UPDATE alembic_version SET version_num = 'deadbeef0000'")
    before = sha(path)

    with pytest.raises(error_class("UnknownDatabaseError")) as raised:
        prepare_database(tmp_path)

    assert str(raised.value) == UNKNOWN_REVISION_TEXT
    assert sha(path) == before
    assert "event=startup_refused reason=unknown_db" in caplog.text


def test_two_revision_rows_are_refused_unchanged(tmp_path):
    path = build_db(tmp_path, "head")
    raw(path, "INSERT INTO alembic_version (version_num) VALUES ('deadbeef0000')")
    before = sha(path)

    with pytest.raises(error_class("UnknownDatabaseError")) as raised:
        prepare_database(tmp_path)

    assert str(raised.value) == UNKNOWN_REVISION_TEXT
    assert sha(path) == before


def test_tables_without_a_revision_stamp_are_refused_unchanged(tmp_path):
    path = tmp_path / "kyapehnu.db"
    raw(path, "CREATE TABLE somebody_elses_table (id INTEGER PRIMARY KEY)")
    before = sha(path)

    with pytest.raises(error_class("UnknownDatabaseError")) as raised:
        prepare_database(tmp_path)

    assert str(raised.value) == UNKNOWN_REVISION_TEXT
    assert sha(path) == before


def test_a_file_that_is_not_sqlite_is_refused_unchanged(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    path = tmp_path / "kyapehnu.db"
    path.write_bytes(bytes(range(256)) * 40)
    before = sha(path)

    with pytest.raises(error_class("UnknownDatabaseError")) as raised:
        prepare_database(tmp_path)

    assert str(raised.value) == UNREADABLE_TEXT
    assert sha(path) == before
    assert "event=startup_refused reason=unreadable_db" in caplog.text


def test_a_truncated_sqlite_file_is_refused_unchanged(tmp_path):
    path = build_db(tmp_path, "head")
    path.write_bytes(path.read_bytes()[:3000])
    before = sha(path)

    with pytest.raises(error_class("UnknownDatabaseError")) as raised:
        prepare_database(tmp_path)

    assert str(raised.value) == UNREADABLE_TEXT
    assert sha(path) == before


def test_create_app_refuses_to_start_on_a_phase3_database(settings, kv):
    from kyapehnu.core.app import create_app

    path = build_db(settings.data_dir, LAST_PHASE3)
    before = sha(path)

    with pytest.raises(error_class("PreOwnershipDatabaseError")):
        create_app(settings=settings, kv=kv, start_worker=False)

    assert sha(path) == before


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from kyapehnu.core.config import get_settings

    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KYAPEHNU_CONFIG_FILE", str(tmp_path / "config.yaml"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_the_migrate_command_refuses_a_phase3_database_with_exit_code_2(cli_env, capsys):
    from kyapehnu.core.cli import main

    path = build_db(cli_env, LAST_PHASE3)
    before = sha(path)

    with pytest.raises(SystemExit) as exit_info:
        main(["migrate"])

    out = capsys.readouterr()
    assert exit_info.value.code == 2
    assert PRE_OWNERSHIP_TEXT in out.out + out.err
    assert sha(path) == before


def test_the_migrate_command_refuses_an_unknown_database_with_exit_code_2(cli_env, capsys):
    from kyapehnu.core.cli import main

    path = build_db(cli_env, "head")
    raw(path, "UPDATE alembic_version SET version_num = 'deadbeef0000'")
    before = sha(path)

    with pytest.raises(SystemExit) as exit_info:
        main(["migrate"])

    out = capsys.readouterr()
    assert exit_info.value.code == 2
    assert UNKNOWN_REVISION_TEXT in out.out + out.err
    assert sha(path) == before


def test_the_migrate_command_still_works_on_a_new_database(cli_env, capsys):
    from kyapehnu.core.cli import main

    main(["migrate"])

    assert "database is up to date" in capsys.readouterr().out


# ---- the revision itself ---------------------------------------------------------------------------------------


def old_profile_row(path: Path) -> None:
    raw(
        path,
        "INSERT INTO userprofile (body_notes, skin_tone, style_prefs, colors_loved, colors_avoided, fit_prefs, "
        "budget_min, budget_max, home_city, id) VALUES ('', '', '', '[]', '[]', '', 1, 2, 'Pune', 1)",
    )


def test_the_revision_refuses_a_database_that_holds_rows(tmp_path):
    path = build_db(tmp_path, LAST_PHASE3)
    old_profile_row(path)
    engine = make_engine(tmp_path)

    with engine.begin() as conn, pytest.raises(RuntimeError, match="needs an empty database"):
        command.upgrade(alembic_config(conn), "head")

    engine.dispose()
    con = sqlite3.connect(path)
    assert con.execute("SELECT version_num FROM alembic_version").fetchall() == [(LAST_PHASE3,)]
    assert con.execute("SELECT COUNT(*) FROM userprofile").fetchone() == (1,)
    con.close()


def test_the_revision_cannot_be_downgraded(tmp_path):
    build_db(tmp_path, "head")
    engine = make_engine(tmp_path)

    with engine.begin() as conn, pytest.raises(NotImplementedError, match="irreversible"):
        command.downgrade(alembic_config(conn), LAST_PHASE3)

    engine.dispose()


@pytest.fixture
def migrated(tmp_path):
    engine = prepare_database(tmp_path)
    yield engine
    engine.dispose()


def test_the_user_table_has_its_constraints_and_the_partial_admin_index(migrated):
    insp = inspect(migrated)

    columns = {c["name"]: c for c in insp.get_columns("user")}
    assert set(columns) == {
        "id",
        "google_sub",
        "email",
        "display_name",
        "avatar_url",
        "is_admin",
        "created_at",
        "last_sign_in_at",
    }
    assert {u["name"]: u["column_names"] for u in insp.get_unique_constraints("user")}["uq_user_google_sub"] == [
        "google_sub"
    ]
    indexes = {i["name"]: i for i in insp.get_indexes("user")}
    assert indexes["ix_user_email"]["column_names"] == ["email"]
    assert indexes["ux_user_one_admin"]["unique"]
    with migrated.connect() as conn:
        ddl = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'ux_user_one_admin'")).scalar()
    assert "WHERE is_admin = 1" in ddl


@pytest.mark.parametrize("table", OWNED_TABLES)
def test_every_owned_table_has_a_required_user_id_with_a_foreign_key(migrated, table):
    insp = inspect(migrated)

    column = next(c for c in insp.get_columns(table) if c["name"] == "user_id")
    assert column["nullable"] is False
    keys = [fk for fk in insp.get_foreign_keys(table) if fk["constrained_columns"] == ["user_id"]]
    assert len(keys) == 1
    assert keys[0]["referred_table"] == "user" and keys[0]["referred_columns"] == ["id"]
    assert keys[0]["name"] == f"fk_{table}_user_id_user"


@pytest.mark.parametrize(("table", "index"), sorted(COMPOSITE_INDEXES.items()))
def test_the_owner_indexes_exist(migrated, table, index):
    name, columns = index

    found = {i["name"]: i["column_names"] for i in inspect(migrated).get_indexes(table)}

    assert found[name] == columns


def test_the_old_indexes_stay(migrated):
    assert {"ix_item_status", "ix_item_category"} <= {i["name"] for i in inspect(migrated).get_indexes("item")}


def test_a_profile_is_unique_per_user(migrated):
    uniques = {u["name"]: u["column_names"] for u in inspect(migrated).get_unique_constraints("userprofile")}

    assert uniques["uq_userprofile_user_id"] == ["user_id"]


def test_the_outfit_key_is_unique_per_user_not_globally(migrated):
    insp = inspect(migrated)

    uniques = {u["name"]: u["column_names"] for u in insp.get_unique_constraints("outfit")}
    assert uniques["uq_outfit_user_id_item_key"] == ["user_id", "item_key"]
    assert "ix_outfit_item_key" not in {i["name"] for i in insp.get_indexes("outfit")}


@pytest.mark.parametrize(
    ("table", "marker"),
    [
        ("item", "ck_item_formality"),
        ("item", "ck_item_warmth"),
        ("toolcall", "ck_toolcall_proposal_state"),
        ("feedback", "ck_feedback_rating"),
        ("chatsession", "AUTOINCREMENT"),
    ],
)
def test_the_recreated_tables_keep_their_check_constraints_and_autoincrement(migrated, table, marker):
    with migrated.connect() as conn:
        ddl = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = :t"), {"t": table}).scalar()

    assert marker in ddl


def test_the_database_enforces_the_owner_foreign_key(migrated):
    with pytest.raises(IntegrityError), migrated.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO userprofile (user_id, body_notes, skin_tone, style_prefs, colors_loved, colors_avoided, "
                "fit_prefs, budget_min, budget_max, home_city) VALUES (999, '', '', '', '[]', '[]', '', 1, 2, 'X')"
            )
        )


def test_the_migrated_schema_is_what_the_models_describe(migrated):
    with migrated.connect() as conn:
        differences = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)

    assert differences == []


def test_the_engine_uses_normal_synchronous_a_five_second_busy_wait_and_foreign_keys(migrated):
    with migrated.connect() as conn:
        assert conn.execute(text("PRAGMA synchronous")).scalar() == 1  # NORMAL
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
