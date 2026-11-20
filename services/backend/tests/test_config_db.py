from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import Engine, MetaData, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from kyapehnu.core import config
from kyapehnu.core.config import PACKAGE_DIR, Prefs, Settings, load_prefs
from kyapehnu.core.db import get_one, make_engine, migrate
from kyapehnu.core.errors import NotFoundError
from kyapehnu.wardrobe.models import Event, EventType, Item, ItemCorrection, ItemSeq, Outfit, OutfitCreator, WearLog
from tests.fakes import OWNER_ID, ensure_owner

MIB = 1024 * 1024
PHASE1_REV = "b2c4e6a81f30"
# The last revision of the single-user chain. The accounts revision (head) refuses a database that holds rows, so the
# tests about the old revisions build and move their databases up to here, not to head.
PHASE3_HEAD = "a7d2e4f39c60"
# Added after Phase 2 by the mannequin and onboarding revisions.
PHASE2_PROFILE_COLUMNS = ("height_cm", "weight_kg", "gender", "name", "age")
PHASE2_TABLES = {"event", "outfit", "wearlog", "itemseq"}
PHASE3_TABLES = {"chatsession", "chatmessage", "toolcall", "feedback"}
WEIGHT_NAMES = ("colour", "pattern", "formality", "weather", "repetition", "forgotten", "preference")
PHASE1_TABLES = ("ingestjob", "photo", "item", "itemcorrection", "ingestpiece")
STAMP = "2026-10-01 10:00:00"


def test_prefs_defaults_when_file_missing(tmp_path):
    p = load_prefs(tmp_path / "nope.yaml")
    assert p.home_city == "Gurugram"
    assert p.currency == "INR"
    assert p.dedupe.llm_confidence == 0.7
    assert p.ingest.min_bbox_area == 0.02


def test_prefs_override_from_yaml(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("home_city: Pune\ndedupe:\n  llm_confidence: 0.9\n")
    p = load_prefs(f)
    assert p.home_city == "Pune"
    assert p.dedupe.llm_confidence == 0.9
    assert p.dedupe.max_delta_e == 15.0


def test_prefs_empty_file_gives_defaults(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("")
    assert load_prefs(f) == config.Prefs()


def test_settings_defaults(monkeypatch):
    for var in (
        "KYAPEHNU_LLM_PROVIDER",
        "ANTHROPIC_API_KEY",
        "KYAPEHNU_ANTHROPIC_API_KEY",
        "KYAPEHNU_DATA_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.llm_provider == "anthropic"
    assert s.anthropic_api_key is None
    assert s.data_dir == Path("/data")
    assert s.config_file == Path("/data/config.yaml")


def test_settings_reads_unprefixed_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert Settings(_env_file=None).anthropic_api_key == "test-key"


def test_upload_limit_constants():
    assert config.MAX_UPLOAD_FILES == 200
    assert config.MAX_UPLOAD_FILE_BYTES == 25 * MIB
    assert config.MAX_BODY_BYTES == 200 * 25 * MIB + MIB


def test_make_engine_creates_nested_data_dir(tmp_path):
    engine = make_engine(tmp_path / "a" / "b")
    migrate(engine)
    assert (tmp_path / "a" / "b" / "kyapehnu.db").is_file()


def test_migrations_create_all_tables(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)
    names = set(inspect(engine).get_table_names())
    assert {"photo", "item", "itemcorrection", "ingestjob", "ingestpiece", "llmcall", "userprofile"} <= names


def test_migrate_twice_is_a_noop(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)
    migrate(engine)
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from alembic_version")).scalar() == 1


def _migrated_for_owner(tmp_path) -> Engine:
    """A migrated database that holds the account the rows of a test belong to."""
    engine = make_engine(tmp_path)
    migrate(engine)
    with Session(engine) as s:
        ensure_owner(s)
        s.commit()
    return engine


@pytest.mark.parametrize("column", ["formality", "warmth"])
@pytest.mark.parametrize("value", [0, 6, 9])
def test_scale_check_constraints(tmp_path, column, value):
    engine = _migrated_for_owner(tmp_path)
    with Session(engine) as s:
        s.add(Item(user_id=OWNER_ID, name="x", category="top", **{column: value}))
        with pytest.raises(IntegrityError):
            s.commit()


@pytest.mark.parametrize("value", [1, 5])
def test_scale_check_accepts_bounds(tmp_path, value):
    engine = _migrated_for_owner(tmp_path)
    with Session(engine) as s:
        s.add(Item(user_id=OWNER_ID, name="x", category="top", formality=value, warmth=value))
        s.commit()


def test_foreign_keys_are_enforced(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)
    with Session(engine) as s:
        s.add(ItemCorrection(item_id=999, field="name"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_json_columns_roundtrip(tmp_path):
    engine = _migrated_for_owner(tmp_path)
    with Session(engine) as s:
        it = Item(
            user_id=OWNER_ID,
            name="Shirt",
            category="top",
            colors=[{"hex": "#ffffff", "name": "white", "role": "primary"}],
            seasons=["summer"],
            tags=["office"],
        )
        s.add(it)
        s.commit()
        s.refresh(it)
        assert it.colors[0]["name"] == "white"
        assert it.seasons == ["summer"]


def test_get_one_returns_row(tmp_path):
    engine = _migrated_for_owner(tmp_path)
    with Session(engine) as s:
        s.add(Item(user_id=OWNER_ID, name="Shirt", category="top"))
        s.commit()
        assert get_one(s, Item, 1).name == "Shirt"


def test_get_one_missing_raises_not_found(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)
    with Session(engine) as s, pytest.raises(NotFoundError) as exc:
        get_one(s, Item, 99)
    assert str(exc.value) == "Item 99 not found"


def test_migration_matches_models(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), SQLModel.metadata) == []


def test_migrations_ship_inside_the_package():
    from kyapehnu.core.config import PACKAGE_DIR

    assert (PACKAGE_DIR / "migrations" / "env.py").is_file()
    assert (PACKAGE_DIR / "migrations" / "script.py.mako").is_file()
    assert any((PACKAGE_DIR / "migrations" / "versions").glob("*.py"))
    assert not (PACKAGE_DIR.parent / "alembic").exists()


def test_version_has_one_source():
    from importlib.metadata import version

    import kyapehnu

    assert kyapehnu.__version__ == version("kyapehnu") == "0.2.0"


# --- Phase 2 migration (AC-1, AC-81) --------------------------------------------------------------------------


def _alembic(engine: Engine, action: str, revision: str) -> None:
    """Same wiring as kyapehnu.core.db.migrate, so the test runs the migration the app runs."""
    cfg = Config()
    cfg.set_main_option("script_location", str(PACKAGE_DIR / "migrations"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        getattr(command, action)(cfg, revision)


def _item_sql(item_id: int, status: str = "available", alt: bool = False) -> str:
    alt_cols = ", alt_cutout_path, alt_thumb_path, picture_generated" if alt else ""
    alt_vals = ", 'cutout/alt.png', 'thumb/alt.webp', 1" if alt else ""
    return (
        f"INSERT INTO item (id, name, category, subcategory, colors, pattern, formality, warmth, seasons, status, "
        f"tags, notes, created_at, updated_at{alt_cols}) VALUES ({item_id}, 'Item {item_id}', 'top', '', '[]', "
        f"'solid', 3, 2, '[\"summer\"]', '{status}', '[]', 'n', '{STAMP}', '{STAMP}'{alt_vals})"
    )


def _phase1_engine(tmp_path) -> Engine:
    """A database exactly as the previous release leaves it: revision b2c4e6a81f30 with rows in every table.
    Item 7 has the clean-picture columns set; the ids have a gap, so max(item.id) is not the row count."""
    engine = make_engine(tmp_path)
    _alembic(engine, "upgrade", PHASE1_REV)
    statements = [
        f"INSERT INTO ingestjob (id, status, source, total, processed, failed, created_at, updated_at) "
        f"VALUES (1, 'done', 'upload', 1, 1, 0, '{STAMP}', '{STAMP}')",
        f"INSERT INTO photo (id, job_id, original_path, photo_type, status, created_at) "
        f"VALUES (1, 1, 'original/a.jpg', 'flat_lay', 'done', '{STAMP}')",
        _item_sql(1),
        _item_sql(2),
        _item_sql(7, status="laundry", alt=True),
        f"INSERT INTO itemcorrection (id, item_id, field, created_at) VALUES (1, 1, 'name', '{STAMP}')",
        f"INSERT INTO ingestpiece (id, job_id, photo_id, bbox, state, created_at, picture_generated, alt_cutout_path, "
        f"item_id) VALUES (1, 1, 1, '[0.1, 0.1, 0.9, 0.9]', 'accepted', '{STAMP}', 1, 'cutout/piece-alt.png', 2)",
    ]
    with engine.begin() as conn:
        for sql in statements:
            conn.execute(text(sql))
    return engine


def _snapshot(engine: Engine) -> dict[str, list[tuple]]:
    with engine.connect() as conn:
        return {t: [tuple(r) for r in conn.execute(text(f"SELECT * FROM {t} ORDER BY id"))] for t in PHASE1_TABLES}


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _insert_phase2_rows(engine: Engine) -> None:
    """Rows in all three FK-linked tables, so a downgrade that drops them in the wrong order fails."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO event (id, title, type, starts_at, place, outdoors, dress_code, audience_tags, notes, "
                f"created_at) VALUES (1, '', 'party', '2026-10-10 18:00:00', 'Pune', 0, '', '[]', '', '{STAMP}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO outfit (id, item_ids, item_key, name, created_by, occasion_tags, created_at) "
                f"VALUES (1, '[1, 2]', '1,2', 'Look', 'agent', '[]', '{STAMP}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO wearlog (id, outfit_id, item_ids, worn_on, event_id, audience_tags, created_at) "
                f"VALUES (1, 1, '[1, 2]', '2026-10-10', 1, '[]', '{STAMP}')"
            )
        )


def _upgraded(tmp_path) -> tuple[Engine, dict[str, list[tuple]]]:
    engine = _phase1_engine(tmp_path)
    before = _snapshot(engine)
    _alembic(engine, "upgrade", PHASE3_HEAD)
    assert PHASE2_TABLES <= _tables(engine), "the upgrade did not run"
    return engine, before


def _downgraded(tmp_path) -> tuple[Engine, dict[str, list[tuple]]]:
    engine, before = _upgraded(tmp_path)
    _insert_phase2_rows(engine)
    _alembic(engine, "downgrade", PHASE1_REV)
    return engine, before


STATES = ["upgraded", "downgraded"]


def _migrated(state: str, tmp_path) -> tuple[Engine, dict[str, list[tuple]]]:
    """The Phase 1 database after the upgrade, or after upgrade plus downgrade, and the rows it held before."""
    return (_upgraded if state == "upgraded" else _downgraded)(tmp_path)


def test_upgrade_over_phase1_data_creates_the_four_tables(tmp_path):
    engine, _ = _upgraded(tmp_path)

    assert PHASE2_TABLES <= _tables(engine)


def test_upgrade_seeds_itemseq_with_the_largest_item_id(tmp_path):
    engine, _ = _upgraded(tmp_path)

    with engine.connect() as conn:
        assert [tuple(r) for r in conn.execute(text("SELECT id, last_id FROM itemseq"))] == [(1, 7)]


def test_upgrade_of_an_empty_database_seeds_itemseq_with_zero(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)

    with engine.connect() as conn:
        assert [tuple(r) for r in conn.execute(text("SELECT id, last_id FROM itemseq"))] == [(1, 0)]


@pytest.mark.parametrize("state", STATES)
def test_phase1_rows_are_unchanged_by_upgrade_and_by_downgrade(state, tmp_path):
    engine, before = _migrated(state, tmp_path)

    assert _snapshot(engine) == before


def test_downgrade_removes_exactly_the_phase2_tables(tmp_path):
    engine, _ = _downgraded(tmp_path)

    assert _tables(engine) & PHASE2_TABLES == set()
    assert set(PHASE1_TABLES) <= _tables(engine)


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize(("formality", "warmth"), [(6, 3), (0, 3), (3, 6), (3, 0)])
def test_item_check_constraints_survive_upgrade_and_downgrade(state, formality, warmth, tmp_path):
    engine, _ = _migrated(state, tmp_path)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(_item_sql(50).replace(", 3, 2,", f", {formality}, {warmth},")),
        )


@pytest.mark.parametrize("state", STATES)
def test_item_indexes_survive_upgrade_and_downgrade(state, tmp_path):
    engine, _ = _migrated(state, tmp_path)

    assert {"ix_item_category", "ix_item_status"} <= {ix["name"] for ix in inspect(engine).get_indexes("item")}


def test_downgraded_database_matches_a_fresh_phase1_database(tmp_path):
    engine, _ = _downgraded(tmp_path / "downgraded")
    fresh = make_engine(tmp_path / "fresh")
    _alembic(fresh, "upgrade", PHASE1_REV)
    phase1 = MetaData()
    phase1.reflect(bind=fresh)

    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), phase1) == []


def test_reupgrade_after_downgrade_restores_the_tables_and_the_seed(tmp_path):
    engine, before = _downgraded(tmp_path)

    _alembic(engine, "upgrade", PHASE3_HEAD)

    assert PHASE2_TABLES <= _tables(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT last_id FROM itemseq WHERE id = 1")).scalar_one() == 7
    assert _snapshot(engine) == before


def test_phase2_indexes_exist_and_the_outfit_key_is_unique_per_user_not_by_index(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)
    insp = inspect(engine)

    def unique_by_name(table: str) -> dict[str, bool]:
        return {ix["name"]: bool(ix["unique"]) for ix in insp.get_indexes(table)}

    assert unique_by_name("event")["ix_event_starts_at"] is False
    assert unique_by_name("event")["ix_event_user_id_starts_at"] is False
    assert "ix_outfit_item_key" not in unique_by_name("outfit")
    assert [(u["name"], u["column_names"]) for u in insp.get_unique_constraints("outfit")] == [
        ("uq_outfit_user_id_item_key", ["user_id", "item_key"])
    ]
    wearlog = unique_by_name("wearlog")
    assert {"ix_wearlog_worn_on", "ix_wearlog_outfit_id", "ix_wearlog_event_id", "ix_wearlog_user_id_worn_on"} <= set(
        wearlog
    )


def test_the_same_user_cannot_save_two_outfits_with_the_same_item_key(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)
    outfit = (
        "INSERT INTO outfit (id, user_id, item_ids, item_key, name, created_by, occasion_tags, created_at) "
        f"VALUES ({{id}}, 1, '[1, 2]', '1,2', 'Look', 'agent', '[]', '{STAMP}')"
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                'INSERT INTO "user" (id, google_sub, email, display_name, is_admin, created_at, last_sign_in_at) '
                f"VALUES (1, 'sub-a', 'a@example.com', 'A', 0, '{STAMP}', '{STAMP}')"
            )
        )
        conn.execute(text(outfit.format(id=1)))

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(outfit.format(id=2)))


def test_a_wear_cannot_point_at_a_missing_event(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO wearlog (id, item_ids, worn_on, event_id, audience_tags, created_at) "
                f"VALUES (1, '[1]', '2026-10-10', 999, '[]', '{STAMP}')"
            )
        )


def test_event_outfit_and_wearlog_rows_roundtrip_with_their_defaults(tmp_path):
    engine = _migrated_for_owner(tmp_path)
    with Session(engine) as s:
        event = Event(
            user_id=OWNER_ID,
            type=EventType.party,
            starts_at=datetime(2026, 10, 10, 18, 0),
            place="Pune",
            audience_tags=["Priya"],
        )
        outfit = Outfit(user_id=OWNER_ID, item_ids=[3, 1], item_key="1,3", name="Look", occasion_tags=["party"])
        s.add_all([event, outfit])
        s.commit()
        s.add(
            WearLog(
                user_id=OWNER_ID,
                outfit_id=outfit.id,
                item_ids=[3, 1],
                worn_on=date(2026, 10, 10),
                event_id=event.id,
                audience_tags=["Priya"],
                weather_snapshot={"source": "forecast", "feels_like_max_c": 26.0},
            )
        )
        s.commit()

    with Session(engine) as s:
        event = s.exec(select(Event)).one()
        outfit = s.exec(select(Outfit)).one()
        wear = s.exec(select(WearLog)).one()
    assert (event.title, event.dress_code, event.notes, event.outdoors, event.ends_at) == ("", "", "", False, None)
    assert event.audience_tags == ["Priya"]
    assert event.starts_at == datetime(2026, 10, 10, 18, 0)  # a naive wall clock at the place, kept as written
    assert outfit.item_ids == [3, 1]
    assert (outfit.created_by, outfit.collage_path, outfit.reason) == (OutfitCreator.agent, None, None)
    assert wear.worn_on == date(2026, 10, 10)
    assert wear.weather_snapshot == {"source": "forecast", "feels_like_max_c": 26.0}


def test_event_types_and_outfit_creators_are_the_specified_sets():
    assert {t.value for t in EventType} == {
        "office",
        "wedding",
        "date",
        "party",
        "festival",
        "travel",
        "casual",
        "gym",
        "interview",
    }
    assert {c.value for c in OutfitCreator} == {"agent", "user"}


# --- item ids are never reused (AC-3, model half) ----------------------------------------------------------------


@pytest.fixture
def owned_engine(engine):
    """The migrated database with the account the items below belong to."""
    with Session(engine) as s:
        ensure_owner(s)
        s.commit()
    return engine


def _new_item(name: str = "x") -> Item:
    return Item(user_id=OWNER_ID, name=name, category="top")


def test_three_items_in_one_flush_get_distinct_increasing_ids(owned_engine):
    with Session(owned_engine) as s:
        items = [_new_item(f"i{n}") for n in range(3)]
        s.add_all(items)
        s.commit()

        assert [i.id for i in items] == [1, 2, 3]


def test_the_item_sequence_records_the_highest_id_issued(owned_engine):
    with Session(owned_engine) as s:
        s.add_all([_new_item(), _new_item()])
        s.commit()

        assert s.get(ItemSeq, 1).last_id == 2


def test_concurrent_creators_each_get_distinct_ids_and_the_sequence_ends_at_the_highest(owned_engine):
    per_thread = 15

    def create(tag):
        ids = []
        for n in range(per_thread):
            with Session(owned_engine) as s:
                item = _new_item(f"{tag}{n}")
                s.add(item)
                s.commit()
                ids.append(item.id)
        return ids

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(create, ["a", "b"])

    assert len({*first, *second}) == 2 * per_thread
    with Session(owned_engine) as s:
        assert s.get(ItemSeq, 1).last_id == max(*first, *second) == 2 * per_thread


def test_a_new_item_never_gets_the_id_of_the_deleted_highest_item(owned_engine):
    with Session(owned_engine) as s:
        s.add_all([_new_item(), _new_item(), _new_item()])
        s.commit()
        s.delete(s.get(Item, 3))
        s.commit()
        new = _new_item()
        s.add(new)
        s.commit()

        assert new.id > 3


def test_a_new_item_never_gets_the_id_of_the_only_deleted_item(owned_engine):
    with Session(owned_engine) as s:
        s.add(_new_item())
        s.commit()
        s.delete(s.get(Item, 1))
        s.commit()
        new = _new_item()
        s.add(new)
        s.commit()

        assert new.id == 2


def test_a_rolled_back_insert_does_not_consume_an_id(owned_engine):
    with Session(owned_engine) as s:
        s.add(_new_item("lost"))
        s.flush()
        s.rollback()
        kept = _new_item("kept")
        s.add(kept)
        s.commit()

        assert kept.id == 1
        assert s.get(ItemSeq, 1).last_id == 1


def test_an_item_inserted_with_an_explicit_id_moves_the_next_id_above_it(owned_engine):
    with Session(owned_engine) as s:
        s.add(Item(id=10, user_id=OWNER_ID, name="fixed", category="top"))
        s.commit()
        new = _new_item()
        s.add(new)
        s.commit()

        assert new.id == 11


def test_the_upgrade_seed_outlives_the_deletion_of_the_highest_item(tmp_path):
    """Phase 1 items 1, 2 and 7: the seed is 7, so after item 7 is deleted the next id is still above 7.
    The ORM insert itself needs a `user_id` column, which this old-schema database does not have; the allocation
    (`last_id` against the table's highest id) is tested at head by the tests above."""
    engine, _ = _upgraded(tmp_path)

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM item WHERE id = 7"))

    with engine.connect() as conn:
        assert conn.execute(text("SELECT max(id) FROM item")).scalar_one() == 2
        assert conn.execute(text("SELECT last_id FROM itemseq WHERE id = 1")).scalar_one() == 7


# --- Phase 2 Prefs (AC-62, AC-11, AC-23) ----------------------------------------------------------------------------


def _load(tmp_path, data: dict) -> Prefs:
    f = tmp_path / "config.yaml"
    f.write_text(yaml.safe_dump(data))
    return load_prefs(f)


def _attr(obj, dotted: str):
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("outfits.per_slot", 8),
        ("outfits.max_candidates", 300),
        ("repetition.half_life_days", 14),
        ("repetition.outfit_window_days", 30),
        ("repetition.audience_window_days", 90),
        ("repetition.forgotten_days", 60),
        ("weather.humid_pct", 70),
        ("weather.humid_feels_like_c", 28),
        ("weather.rain_probability_pct", 40),
        ("weather.uv_index", 8),
        ("weather.aqi", 200),
        ("weather.wind_kmh", 35),
        ("weather.default_event_hours", 3),
        ("weather.humid_fabrics", ["polyester", "nylon", "wool", "leather", "velvet"]),
        ("weather.not_rain_safe_words", ["suede", "canvas", "satin", "silk", "velvet", "mesh"]),
    ],
)
def test_phase2_prefs_defaults_apply_when_the_file_has_no_phase2_keys(tmp_path, path, expected):
    prefs = _load(tmp_path, {"home_city": "Pune"})

    value = _attr(prefs, path)
    assert (list(value) if isinstance(expected, list) else value) == expected


def test_default_weights_are_the_specified_values_and_sum_to_one():
    w = Prefs().outfits.weights

    values = [getattr(w, name) for name in WEIGHT_NAMES]
    assert values == pytest.approx([v / 1.10 for v in (0.25, 0.10, 0.20, 0.20, 0.20, 0.05, 0.10)])  # normalised
    assert sum(values) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("gym", (1, 1)),
        ("casual", (1, 3)),
        ("travel", (1, 3)),
        ("date", (2, 4)),
        ("party", (2, 4)),
        ("festival", (3, 5)),
        ("office", (3, 4)),
        ("interview", (4, 5)),
        ("wedding", (4, 5)),
    ],
)
def test_default_formality_range_per_event_type(event_type, expected):
    assert tuple(getattr(Prefs().outfits.formality, event_type)) == expected


def test_a_partial_formality_override_keeps_the_other_event_types(tmp_path):
    prefs = _load(tmp_path, {"outfits": {"formality": {"office": [2, 4]}}})

    assert tuple(prefs.outfits.formality.office) == (2, 4)
    assert tuple(prefs.outfits.formality.wedding) == (4, 5)
    assert tuple(prefs.outfits.formality.gym) == (1, 1)


def test_default_weather_bands_are_the_specified_table():
    bands = [(b.from_c, b.warmth_min, b.warmth_max, b.layer) for b in Prefs().weather.bands]

    assert bands == [
        (30, 1, 2, "none"),
        (24, 1, 3, "none"),
        (18, 2, 4, "optional"),
        (10, 3, 5, "wanted"),
        (None, 4, 5, "wanted"),
    ]


def test_weights_are_normalised_to_sum_one_keeping_their_ratios(tmp_path):
    weights = {
        "colour": 2,
        "pattern": 2,
        "formality": 2,
        "weather": 2,
        "repetition": 1,
        "forgotten": 1,
        "preference": 0,
    }

    w = _load(tmp_path, {"outfits": {"weights": weights}}).outfits.weights

    assert [w.colour, w.pattern, w.formality, w.weather, w.repetition, w.forgotten] == pytest.approx(
        [0.2, 0.2, 0.2, 0.2, 0.1, 0.1]
    )


def test_the_default_preference_weight_joins_the_sum_when_the_file_does_not_set_it(tmp_path):
    weights = {"colour": 2, "pattern": 2, "formality": 2, "weather": 2, "repetition": 1, "forgotten": 1}

    w = _load(tmp_path, {"outfits": {"weights": weights}}).outfits.weights

    assert [getattr(w, name) for name in WEIGHT_NAMES] == pytest.approx([v / 10.1 for v in (2, 2, 2, 2, 1, 1, 0.1)])


def test_the_example_config_lists_all_seven_weights_at_their_defaults():
    raw = yaml.safe_load((Path(__file__).parents[1] / "config.example.yaml").read_text())

    assert raw["outfits"]["weights"] == {
        "colour": 0.25,
        "pattern": 0.10,
        "formality": 0.20,
        "weather": 0.20,
        "repetition": 0.20,
        "forgotten": 0.05,
        "preference": 0.10,
    }


@pytest.mark.parametrize("name", WEIGHT_NAMES)
def test_negative_weight_fails_naming_the_key(tmp_path, name):
    with pytest.raises(ValidationError) as exc:
        _load(tmp_path, {"outfits": {"weights": {name: -1}}})

    assert name in str(exc.value)


@pytest.mark.parametrize(
    ("data", "key"),
    [
        (
            {"outfits": {"weights": dict.fromkeys(WEIGHT_NAMES, 0)}},
            "weights",
        ),
        ({"weather": {"rain_probability_pct": "lots"}}, "rain_probability_pct"),
        ({"weather": {"wind_kmh": "breezy"}}, "wind_kmh"),
        ({"outfits": {"per_slot": 0}}, "per_slot"),
        ({"outfits": {"per_slot": 21}}, "per_slot"),
        ({"outfits": {"max_candidates": 0}}, "max_candidates"),
        ({"outfits": {"max_candidates": 1001}}, "max_candidates"),
        ({"outfits": {"formality": {"office": [5, 3]}}}, "office"),
        ({"outfits": {"formality": {"office": [0, 3]}}}, "office"),
        ({"outfits": {"formality": {"gym": [1, 6]}}}, "gym"),
        ({"repetition": {"half_life_days": 0}}, "half_life_days"),
        ({"repetition": {"outfit_window_days": 0}}, "outfit_window_days"),
        ({"repetition": {"audience_window_days": 0}}, "audience_window_days"),
        ({"repetition": {"forgotten_days": 0}}, "forgotten_days"),
        ({"weather": {"default_event_hours": 0}}, "default_event_hours"),
        ({"weather": {"default_event_hours": 25}}, "default_event_hours"),
    ],
)
def test_an_invalid_phase2_value_fails_the_load_and_names_the_key(tmp_path, data, key):
    with pytest.raises(ValidationError) as exc:
        _load(tmp_path, data)

    assert key in str(exc.value)


@pytest.mark.parametrize(
    "data",
    [
        {"outfits": {"per_slot": 1, "max_candidates": 1}},
        {"outfits": {"per_slot": 20, "max_candidates": 1000}},
        {"weather": {"default_event_hours": 1}},
        {"weather": {"default_event_hours": 24}},
        {"outfits": {"formality": {"gym": [1, 5]}}},
        {
            "outfits": {
                "weights": {"colour": 1, "pattern": 0, "formality": 0, "weather": 0, "repetition": 0, "forgotten": 0}
            }
        },
    ],
)
def test_phase2_values_at_their_limits_are_accepted(tmp_path, data):
    _load(tmp_path, data)


def _band(from_c, lo=1, hi=2, layer="none") -> dict:
    return {"from_c": from_c, "warmth_min": lo, "warmth_max": hi, "layer": layer}


@pytest.mark.parametrize(
    "bands",
    [
        [_band(10), _band(20), _band(None)],  # ascending
        [_band(20), _band(20), _band(None)],  # not strictly descending
        [_band(20), _band(10)],  # the last band is not the open one
        [_band(None), _band(None)],  # an open band before the last
        [_band(20, lo=0), _band(None)],  # warmth below 1
        [_band(20, hi=6), _band(None)],  # warmth above 5
        [_band(20, lo=4, hi=2), _band(None)],  # min above max
    ],
)
def test_invalid_weather_bands_fail_the_load_and_name_the_key(tmp_path, bands):
    with pytest.raises(ValidationError) as exc:
        _load(tmp_path, {"weather": {"bands": bands}})

    assert "bands" in str(exc.value)


def test_custom_weather_bands_replace_the_defaults(tmp_path):
    bands = [_band(20, 1, 3, "optional"), _band(None, 3, 5, "wanted")]

    prefs = _load(tmp_path, {"weather": {"bands": bands}})

    assert [(b.from_c, b.warmth_min, b.warmth_max, b.layer) for b in prefs.weather.bands] == [
        (20, 1, 3, "optional"),
        (None, 3, 5, "wanted"),
    ]
