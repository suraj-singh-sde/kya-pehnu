"""Migration e5b8c2d17a40 (AC-1, LLD 5.2, 5.3): four new tables over a Phase 2 database, nothing else touched."""

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from kyapehnu.agent.models import ChatMessage, ChatRole, ChatSession, ProposalState, ToolCall
from kyapehnu.core.config import PACKAGE_DIR
from kyapehnu.core.db import HEAD_REVISION, OWNERSHIP_REVISION, make_engine, migrate

PHASE2_HEAD = "d4f1a7c90b22"
PHASE3_REVISION = "e5b8c2d17a40"
GENDER_REVISION = "f6c9d3e28b51"  # profile gender, after phase 3
LAST_SINGLE_USER_REVISION = "a7d2e4f39c60"  # name and age, asked at onboarding: the last revision before accounts
USER_ID = 1
PHASE3_TABLES = {"chatsession", "chatmessage", "toolcall", "feedback"}
STAMP = "2026-10-01 10:00:00"

# column -> nullable, as LLD 5.2 lists them
COLUMNS = {
    "chatsession": {"id": False, "title": False, "created_at": False, "updated_at": False, "user_id": False},
    "chatmessage": {
        "id": False,
        "session_id": False,
        "turn": False,
        "step": False,
        "role": False,
        "content": False,
        "created_at": False,
        "user_id": False,
    },
    "toolcall": {
        "id": False,
        "session_id": False,
        "turn": False,
        "step": False,
        "call_id": False,
        "name": False,
        "args": True,
        "result": False,
        "truncated": False,
        "error": True,
        "latency_ms": False,
        "event_id": True,
        "ui": True,
        "proposal_state": True,
        "proposed_on": True,
        "decided_at": True,
        "outcome": True,
        "created_at": False,
        "user_id": False,
    },
    "feedback": {
        "id": False,
        "outfit_id": False,
        "rating": False,
        "reason": False,
        "created_at": False,
        "user_id": False,
    },
}
INDEXES = {
    "chatsession": {
        "ix_chatsession_updated_at": ["updated_at"],
        "ix_chatsession_user_id_updated_at": ["user_id", "updated_at"],
    },
    "chatmessage": {"ix_chatmessage_session_id_id": ["session_id", "id"], "ix_chatmessage_user_id": ["user_id"]},
    "toolcall": {
        "ix_toolcall_session_id_id": ["session_id", "id"],
        "ix_toolcall_event_id": ["event_id"],
        "ix_toolcall_user_id_id": ["user_id", "id"],
    },
    "feedback": {"ix_feedback_outfit_id_id": ["outfit_id", "id"], "ix_feedback_user_id": ["user_id"]},
}


def _alembic(engine: Engine, action: str, revision: str) -> None:
    """Same wiring as kyapehnu.core.db.migrate, so the test runs the migration the app runs."""
    cfg = Config()
    cfg.set_main_option("script_location", str(PACKAGE_DIR / "migrations"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        getattr(command, action)(cfg, revision)


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _phase2_engine(tmp_path) -> Engine:
    """A database exactly as the previous release leaves it: revision d4f1a7c90b22 with a row in every table.

    Items 1 and 2 exist and item 3 was deleted, so itemseq.last_id (3) is not max(item.id) (2). The models now have
    columns these revisions lack, so every row goes in as SQL."""
    engine = make_engine(tmp_path)
    _alembic(engine, "upgrade", PHASE2_HEAD)
    item = (
        "INSERT INTO item (id, name, category, subcategory, colors, pattern, formality, warmth, seasons, status, "
        f"tags, notes, created_at, updated_at) VALUES ({{id}}, 'Item {{id}}', 'top', '', '[]', 'solid', 3, 2, '[]', "
        f"'available', '[\"a\"]', '', '{STAMP}', '{STAMP}')"
    )
    statements = [
        "INSERT INTO userprofile (id, body_notes, skin_tone, style_prefs, colors_loved, colors_avoided, "
        "fit_prefs, budget_min, budget_max, home_city) "
        "VALUES (1, '', '', '', '[]', '[]', '', 1000, 5000, 'Pune')",
        f"INSERT INTO ingestjob (id, status, source, total, processed, failed, created_at, updated_at) "
        f"VALUES (1, 'done', 'upload', 1, 1, 0, '{STAMP}', '{STAMP}')",
        item.format(id=1),
        item.format(id=2),
        item.format(id=3),
        "DELETE FROM item WHERE id = 3",
        "UPDATE itemseq SET last_id = 3 WHERE id = 1",
        "INSERT INTO event (id, title, type, starts_at, place, outdoors, dress_code, audience_tags, notes, created_at) "
        f"VALUES (1, 'Dinner', 'party', '2026-10-10 19:00:00', 'Pune', 0, '', '[]', '', '{STAMP}')",
        "INSERT INTO outfit (id, item_ids, item_key, name, created_by, occasion_tags, created_at) "
        f"VALUES (1, '[1, 2]', '1,2', 'Look', 'agent', '[]', '{STAMP}')",
        "INSERT INTO llmcall (id, provider, model, purpose, request_summary, latency_ms, created_at) "
        f"VALUES (1, 'p', 'm', 'tagging', 'x', 1, '{STAMP}')",
        "INSERT INTO wearlog (id, outfit_id, item_ids, worn_on, event_id, audience_tags, created_at) "
        f"VALUES (1, 1, '[1, 2]', '2026-10-10', 1, '[]', '{STAMP}')",
    ]
    with engine.begin() as conn:
        for sql in statements:
            conn.execute(text(sql))
    return engine


ONBOARDING_COLUMNS = {"gender", "name", "age"}


def _phase2_columns(conn: Connection, table: str) -> str:
    cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))]
    return ", ".join(c for c in cols if not (table == "userprofile" and c in ONBOARDING_COLUMNS))


def _snapshot(engine: Engine) -> dict[str, object]:
    """Every row of every table that is not new, every index on them, and itemseq."""
    with engine.connect() as conn:
        names = [
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            )
        ]
        old = sorted(n for n in names if n not in PHASE3_TABLES and n != "alembic_version")
        # The onboarding revisions add profile columns after Phase 3; the rows must match without them.
        snapshot: dict[str, object] = {
            n: [tuple(r) for r in conn.execute(text(f"SELECT {_phase2_columns(conn, n)} FROM {n} ORDER BY 1"))]
            for n in old
        }
        snapshot["indexes"] = sorted(
            tuple(r)
            for r in conn.execute(text("SELECT tbl_name, name, sql FROM sqlite_master WHERE type = 'index'"))
            if r[0] in old
        )
        snapshot["tables"] = old
    return snapshot


def _fill_phase3(engine: Engine) -> None:
    """A row in each new table, with every foreign key set, so a downgrade that drops parents first fails."""
    statements = [
        f"INSERT INTO chatsession (id, title, created_at, updated_at) VALUES (1, 'Dinner', '{STAMP}', '{STAMP}')",
        "INSERT INTO chatmessage (session_id, turn, step, role, content, created_at) "
        f"VALUES (1, 1, 0, 'user', 'hi', '{STAMP}')",
        _tool_call_sql("NULL", event_id="1", user_id=None),
        f"INSERT INTO feedback (outfit_id, rating, reason, created_at) VALUES (1, 1, 'nice', '{STAMP}')",
    ]
    with engine.begin() as conn:
        for sql in statements:
            conn.execute(text(sql))


# --- the revision ------------------------------------------------------------------------------------------------


def test_the_phase_3_revision_follows_the_phase_2_head_and_the_accounts_revision_is_the_only_head():
    script = ScriptDirectory(str(PACKAGE_DIR / "migrations"))

    assert script.get_heads() == [HEAD_REVISION]
    assert script.get_revision(OWNERSHIP_REVISION).down_revision == LAST_SINGLE_USER_REVISION
    assert script.get_revision(LAST_SINGLE_USER_REVISION).down_revision == GENDER_REVISION
    assert script.get_revision(GENDER_REVISION).down_revision == PHASE3_REVISION
    assert script.get_revision(PHASE3_REVISION).down_revision == PHASE2_HEAD


def test_phase2_db_upgrades_keeps_rows_and_itemseq_then_downgrades_and_reupgrades(tmp_path):
    engine = _phase2_engine(tmp_path)
    before = _snapshot(engine)
    assert before["itemseq"] == [(1, 3)]
    assert not PHASE3_TABLES & _tables(engine)

    _alembic(engine, "upgrade", LAST_SINGLE_USER_REVISION)

    assert PHASE3_TABLES <= _tables(engine)
    assert _snapshot(engine) == before
    _fill_phase3(engine)

    _alembic(engine, "downgrade", PHASE2_HEAD)

    assert not PHASE3_TABLES & _tables(engine)
    assert _snapshot(engine) == before

    _alembic(engine, "upgrade", LAST_SINGLE_USER_REVISION)

    assert PHASE3_TABLES <= _tables(engine)
    assert _snapshot(engine) == before
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 1
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == LAST_SINGLE_USER_REVISION


def test_the_upgrade_leaves_itemseq_alone_so_the_next_item_still_gets_a_fresh_id(tmp_path):
    engine = _phase2_engine(tmp_path)

    _alembic(engine, "upgrade", LAST_SINGLE_USER_REVISION)

    with engine.connect() as conn:  # the next id is above both: 3 was deleted and is never reused
        assert conn.execute(text("SELECT max(id) FROM item")).scalar_one() == 2
        assert conn.execute(text("SELECT last_id FROM itemseq WHERE id = 1")).scalar_one() == 3


def test_a_fresh_database_gets_the_four_empty_tables(tmp_path):
    engine = make_engine(tmp_path)

    migrate(engine)

    with engine.connect() as conn:
        assert {
            t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in PHASE3_TABLES
        } == dict.fromkeys(PHASE3_TABLES, 0)


# --- the tables (LLD 5.2) --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(COLUMNS))
def test_each_table_has_the_specified_columns_and_nullability(table, tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)

    columns = {c["name"]: c["nullable"] for c in inspect(engine).get_columns(table)}

    assert columns == COLUMNS[table]


@pytest.mark.parametrize("table", sorted(INDEXES))
def test_each_table_has_its_named_indexes_and_no_others(table, tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)

    indexes = {ix["name"]: ix["column_names"] for ix in inspect(engine).get_indexes(table)}

    assert indexes == INDEXES[table]


def test_foreign_keys_point_where_the_design_says_and_nowhere_at_item(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)
    insp = inspect(engine)

    targets = {
        table: sorted((tuple(fk["constrained_columns"]), fk["referred_table"]) for fk in insp.get_foreign_keys(table))
        for table in COLUMNS
    }

    assert targets == {
        "chatsession": [(("user_id",), "user")],
        "chatmessage": [(("session_id",), "chatsession"), (("user_id",), "user")],
        "toolcall": [(("event_id",), "event"), (("session_id",), "chatsession"), (("user_id",), "user")],
        "feedback": [(("outfit_id",), "outfit"), (("user_id",), "user")],
    }


# --- constraints --------------------------------------------------------------------------------------------------


def _insert(engine: Engine, sql: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql))


def _tool_call_sql(
    proposal_state: str, session_id: int = 1, event_id: str = "NULL", user_id: int | None = USER_ID
) -> str:
    """`user_id=None` is the row of the single-user schema, which has no such column."""
    owner_column, owner_value = ("", "") if user_id is None else (", user_id", f", {user_id}")
    return (
        "INSERT INTO toolcall (session_id, turn, step, call_id, name, result, truncated, latency_ms, event_id, "
        f"proposal_state, created_at{owner_column}) VALUES ({session_id}, 1, 1, 'c1', 'log_wear', '{{}}', 0, 1, "
        f"{event_id}, {proposal_state}, '{STAMP}'{owner_value})"
    )


def _add_user(engine: Engine) -> None:
    _insert(
        engine,
        'INSERT INTO "user" (id, google_sub, email, display_name, is_admin, created_at, last_sign_in_at) '
        f"VALUES ({USER_ID}, 'sub-a', 'a@example.com', 'A', 0, '{STAMP}', '{STAMP}')",
    )


@pytest.fixture
def at_head(tmp_path) -> Engine:
    """A new database at head with one account, the owner of every row these tests insert."""
    engine = make_engine(tmp_path)
    migrate(engine)
    _add_user(engine)
    return engine


@pytest.fixture
def migrated(at_head) -> Engine:
    _insert(
        at_head,
        "INSERT INTO chatsession (id, user_id, title, created_at, updated_at) "
        f"VALUES (1, {USER_ID}, '', '{STAMP}', '{STAMP}')",
    )
    _insert(
        at_head,
        "INSERT INTO event (id, user_id, title, type, starts_at, place, outdoors, dress_code, audience_tags, notes, "
        f"created_at) VALUES (1, {USER_ID}, '', 'party', '2026-10-10 18:00:00', 'Pune', 0, '', '[]', '', '{STAMP}')",
    )
    _insert(
        at_head,
        "INSERT INTO outfit (id, user_id, item_ids, item_key, name, created_by, occasion_tags, created_at) "
        f"VALUES (1, {USER_ID}, '[1, 2]', '1,2', 'Look', 'agent', '[]', '{STAMP}')",
    )
    return at_head


@pytest.mark.parametrize("state", ["NULL", "'pending'", "'confirmed'", "'cancelled'", "'expired'"])
def test_a_tool_call_proposal_state_may_be_empty_or_one_of_the_four(migrated, state):
    _insert(migrated, _tool_call_sql(state))


@pytest.mark.parametrize("state", ["'done'", "'Pending'", "''"])
def test_any_other_proposal_state_is_refused(migrated, state):
    with pytest.raises(IntegrityError):
        _insert(migrated, _tool_call_sql(state))


@pytest.mark.parametrize("rating", [1, -1])
def test_a_feedback_rating_of_plus_or_minus_one_is_stored(migrated, rating):
    _insert(
        migrated,
        f"INSERT INTO feedback (user_id, outfit_id, rating, reason, created_at) "
        f"VALUES ({USER_ID}, 1, {rating}, '', '{STAMP}')",
    )


@pytest.mark.parametrize("rating", [0, 2, -2])
def test_any_other_feedback_rating_is_refused(migrated, rating):
    with pytest.raises(IntegrityError):
        _insert(
            migrated,
            "INSERT INTO feedback (user_id, outfit_id, rating, reason, created_at) "
            f"VALUES ({USER_ID}, 1, {rating}, '', '{STAMP}')",
        )


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO chatmessage (user_id, session_id, turn, step, role, content, created_at) "
        f"VALUES ({USER_ID}, 99, 1, 0, 'user', 'x', '{STAMP}')",
        _tool_call_sql("NULL", session_id=99),
        _tool_call_sql("NULL", event_id="99"),
        "INSERT INTO feedback (user_id, outfit_id, rating, reason, created_at) "
        f"VALUES ({USER_ID}, 99, 1, '', '{STAMP}')",
    ],
    ids=["message-session", "toolcall-session", "toolcall-event", "feedback-outfit"],
)
def test_a_row_pointing_at_a_missing_parent_is_refused(migrated, sql):
    with pytest.raises(IntegrityError):
        _insert(migrated, sql)


# --- ids of deleted chats are never reused (gov low 4, WP-Q2) ---------------------------------------------------------


def _new_session(engine: Engine, title: str) -> int:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO chatsession (user_id, title, created_at, updated_at) VALUES (:u, :t, :s, :s)"),
            {"u": USER_ID, "t": title, "s": STAMP},
        )
        return conn.execute(text("SELECT max(id) FROM chatsession")).scalar_one()


def test_a_deleted_newest_chat_session_never_gives_its_id_to_the_next_one(at_head):
    engine = at_head
    assert [_new_session(engine, "a"), _new_session(engine, "b")] == [1, 2]
    _insert(engine, "DELETE FROM chatsession WHERE id = 2")

    assert _new_session(engine, "c") == 3


def test_the_migrated_chat_session_table_keeps_its_counter_in_sqlite_sequence(at_head):
    engine = at_head
    _new_session(engine, "a")

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT seq FROM sqlite_sequence WHERE name = 'chatsession'")).all()

    assert [tuple(r) for r in rows] == [(1,)]


def test_the_model_alone_also_never_reuses_a_chat_session_id():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        first, second = ChatSession(user_id=USER_ID, title="a"), ChatSession(user_id=USER_ID, title="b")
        s.add_all([first, second])
        s.commit()
        assert (first.id, second.id) == (1, 2)
        s.delete(second)
        s.commit()
        third = ChatSession(user_id=USER_ID, title="c")
        s.add(third)
        s.commit()

        assert third.id == 3


# --- the enums the rows use -------------------------------------------------------------------------------------------


def test_chat_roles_and_proposal_states_are_the_specified_strings():
    assert [r.value for r in ChatRole] == ["user", "assistant", "notice"]
    assert [p.value for p in ProposalState] == ["pending", "confirmed", "cancelled", "expired"]


def test_a_chat_row_roundtrips_through_the_models_with_its_defaults(at_head):
    engine = at_head
    with Session(engine) as s:
        chat = ChatSession(user_id=USER_ID)
        s.add(chat)
        s.commit()
        s.add(
            ToolCall(
                user_id=USER_ID,
                session_id=chat.id,
                turn=1,
                step=1,
                call_id="c1",
                name="get_item",
                result="{}",
                latency_ms=3,
            )
        )
        s.add(ChatMessage(user_id=USER_ID, session_id=chat.id, turn=1, step=0, role=ChatRole.user, content="hi"))
        s.commit()
        s.refresh(chat)

        call = s.exec(select(ToolCall)).one()
        message = s.exec(select(ChatMessage)).one()

        assert chat.title == "" and chat.created_at is not None and chat.updated_at is not None
        assert (call.truncated, call.args, call.error, call.event_id, call.proposal_state) == (
            False,
            None,
            None,
            None,
            None,
        )
        assert (call.ui, call.proposed_on, call.decided_at, call.outcome) == (None, None, None, None)
        assert message.role == ChatRole.user and message.created_at is not None
