"""kyapehnu CLI: migrate, seed, backup, openapi and the commands that arrive later (AC-5, AC-32, AC-33, AC-36)."""

import json
import re
import sqlite3
import threading
import zipfile
from pathlib import Path

import pytest
from sqlmodel import Session, func, select

from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.core.cli import main
from kyapehnu.core.config import get_settings
from kyapehnu.core.db import make_engine, prepare_database, utcnow
from kyapehnu.wardrobe.models import Item
from kyapehnu.wardrobe.seed import SEED_ITEMS

FRONTEND_DIR = Path(__file__).parents[2] / "frontend"  # services/frontend

# The routes and schema names the generated frontend types depend on (plan Task 7, spec section 6).
CONTRACT_PATHS = {
    "/api/items",
    "/api/items/{item_id}",
    "/api/items/{item_id}/status",
    "/api/ingest",
    "/api/ingest/jobs",
    "/api/ingest/jobs/{job_id}",
    "/api/ingest/pieces",
    "/api/ingest/pieces/{piece_id}/accept",
    "/api/ingest/pieces/{piece_id}/discard",
    "/api/ingest/pieces/{piece_id}/merge",
    "/api/ingest/pieces/{piece_id}/mark-duplicate",
    "/api/ingest/photos/{photo_id}/retry",
    "/api/profile",
    "/api/settings",
    "/api/settings/test-llm",
    "/api/debug/llm-calls",
    # Phase 2 (wear log, outfits, events, items)
    "/api/outfits",
    "/api/outfits/suggest",
    "/api/outfits/swap",
    "/api/outfits/wear",
    "/api/events",
    "/api/items/status",
    "/api/items/{item_id}/worn-today",
    # Phase 3 (chat, feedback, weather, wear history)
    "/api/chat/sessions",
    "/api/chat/sessions/{session_id}",
    "/api/chat/sessions/{session_id}/messages",
    "/api/chat/proposals/{tool_call_id}/confirm",
    "/api/chat/proposals/{tool_call_id}/cancel",
    "/api/feedback",
    "/api/weather",
    "/api/wears",
    "/api/debug/tool-calls",
}
CONTRACT_SCHEMAS = {
    "Item",
    "ItemDetail",
    "IngestPiece",
    "IngestJob",
    "LlmCall",
    "UserProfile",
    "UserProfileBase",
    "SettingsOut",
    "TagsPatch",
    "ItemPatch",
    "AcceptIn",
    "MergeIn",
    "DuplicateIn",
    "StatusIn",
    "TestLlmOut",
    # Phase 2
    "SuggestIn",
    "SuggestOut",
    "SuggestionOut",
    "SwapIn",
    "SwapHint",
    "WearIn",
    "WearLogOut",
    "OutfitOut",
    "WeatherOut",
    "WeatherSummary",
    "Problem",
    "Parts",
    "Event",
    "EventIn",
    "BulkStatusIn",
    # Phase 3
    "SessionSummary",
    "MessageIn",
    "MessageOut",
    "CardSuggestion",
    "OutfitsCard",
    "ProposalView",
    "ToolCallView",
    "SessionOut",
    "TurnOut",
    "FeedbackIn",
    "FeedbackOut",
    "EventDraft",
    "WeatherReport",
    "ToolCall",
}


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """Point the CLI at a throwaway data dir and config, whatever the developer's .env says."""
    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYAPEHNU_CONFIG_FILE", str(tmp_path / "config.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    yield tmp_path / "data"
    get_settings.cache_clear()


ACCOUNT = "seed.owner@example.com"


def _account(data_dir) -> int:
    """The account `kyapehnu seed` and `import-folder` name by email. Returns its id."""
    engine = prepare_database(data_dir)
    try:
        with Session(engine) as s:
            return upsert_google_user(s, sub="sub-cli", email=ACCOUNT, name="cli", now=utcnow()).id
    finally:
        engine.dispose()


def _item_count(data_dir) -> int:
    with Session(make_engine(data_dir)) as s:
        return s.exec(select(func.count()).select_from(Item)).one()


# --- migrate, seed, backup (AC-5, AC-32, AC-33) -------------------------------------------------------------


def test_cli_migrate_creates_all_tables_and_is_idempotent(data_dir, capsys):
    main(["migrate"])
    main(["migrate"])

    assert capsys.readouterr().out.count("database is up to date") == 2
    with sqlite3.connect(data_dir / "kyapehnu.db") as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"item", "photo", "ingestjob", "ingestpiece", "itemcorrection", "llmcall", "userprofile"} <= tables
    assert (data_dir / "images").is_dir()


def test_cli_seed_adds_items_then_refuses_without_force(data_dir, capsys):
    _account(data_dir)
    main(["seed", "--email", ACCOUNT])
    assert f"added {len(SEED_ITEMS)} seed items" in capsys.readouterr().out
    assert _item_count(data_dir) == len(SEED_ITEMS)

    with pytest.raises(SystemExit) as exc:
        main(["seed", "--email", ACCOUNT])

    assert isinstance(exc.value.code, str) and "--force" in exc.value.code
    assert _item_count(data_dir) == len(SEED_ITEMS)


def test_cli_seed_force_adds_again(data_dir, capsys):
    _account(data_dir)
    main(["seed", "--email", ACCOUNT])
    main(["seed", "--email", ACCOUNT, "--force"])

    assert _item_count(data_dir) == 2 * len(SEED_ITEMS)
    assert capsys.readouterr().out.count("seed items") == 2


def test_cli_backup_prints_path_and_size_of_a_zip_with_the_database(data_dir, capsys):
    _account(data_dir)
    main(["seed", "--email", ACCOUNT])
    capsys.readouterr()

    main(["backup"])

    match = re.fullmatch(r"backup written: (.+) \((\d+\.\d) MB\)\n", capsys.readouterr().out)
    assert match, "expected one line: backup written: <path> (<n.n> MB)"
    archive = Path(match.group(1))
    assert archive.parent == data_dir / "backups" and archive.suffix == ".zip"
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
    assert "kyapehnu.db" in names and any(n.startswith("images/1/cutout/") for n in names)


# --- openapi (AC-36) ----------------------------------------------------------------------------------------


def test_cli_openapi_emits_the_schema_the_frontend_types_are_generated_from(data_dir, capsys):
    main(["openapi"])

    schema = json.loads(capsys.readouterr().out)  # stdout is the schema and nothing else
    assert "/api/items" in schema["paths"]
    assert CONTRACT_PATHS <= set(schema["paths"])
    assert CONTRACT_SCHEMAS <= set(schema["components"]["schemas"])


def test_cli_openapi_needs_no_data_directory(data_dir, capsys):
    main(["openapi"])

    assert json.loads(capsys.readouterr().out)["paths"]
    assert not data_dir.exists()  # KYAPEHNU_DATA_DIR points here and nothing was created


PHASE2_OPERATIONS = [
    ("/api/outfits/suggest", "post", "200"),
    ("/api/outfits/swap", "post", "200"),
    ("/api/outfits/wear", "post", "201"),
    ("/api/outfits", "get", "200"),
    ("/api/events", "get", "200"),
    ("/api/items/{item_id}/worn-today", "post", "201"),
    ("/api/items/status", "post", "200"),
]


@pytest.mark.parametrize(("path", "method", "code"), PHASE2_OPERATIONS)
def test_cli_openapi_declares_each_phase_2_operation_with_its_success_code(data_dir, capsys, path, method, code):
    main(["openapi"])

    operation = json.loads(capsys.readouterr().out)["paths"][path][method]
    assert code in operation["responses"]


def test_cli_openapi_carries_the_phase_2_fields_the_frontend_reads(data_dir, capsys):
    main(["openapi"])

    schema = json.loads(capsys.readouterr().out)
    schemas = schema["components"]["schemas"]
    assert {"wears", "outfits"} <= set(schemas["ItemDetail"]["properties"])
    assert {"outfits", "repetition", "weather"} <= set(schemas["SettingsOut"]["properties"])
    assert "not_worn_days" in {p["name"] for p in schema["paths"]["/api/items"]["get"]["parameters"]}


PHASE3_OPERATIONS = [
    ("/api/chat/sessions", "get", "200"),
    ("/api/chat/sessions", "post", "201"),
    ("/api/chat/sessions/{session_id}", "get", "200"),
    ("/api/chat/sessions/{session_id}", "delete", "204"),
    ("/api/chat/sessions/{session_id}/messages", "post", "200"),
    ("/api/chat/proposals/{tool_call_id}/confirm", "post", "200"),
    ("/api/chat/proposals/{tool_call_id}/cancel", "post", "200"),
    ("/api/feedback", "post", "201"),
    ("/api/events", "post", "201"),
    ("/api/weather", "get", "200"),
    ("/api/wears", "get", "200"),
    ("/api/debug/tool-calls", "get", "200"),
]


@pytest.mark.parametrize(("path", "method", "code"), PHASE3_OPERATIONS)
def test_cli_openapi_declares_each_phase_3_operation_with_its_success_code(data_dir, capsys, path, method, code):
    main(["openapi"])

    operation = json.loads(capsys.readouterr().out)["paths"][path][method]
    assert code in operation["responses"]


def _query_names(schema: dict, path: str, method: str = "get") -> set[str]:
    return {p["name"] for p in schema["paths"][path][method]["parameters"] if p["in"] == "query"}


def test_cli_openapi_carries_the_phase_3_fields_the_frontend_reads(data_dir, capsys):
    main(["openapi"])

    schema = json.loads(capsys.readouterr().out)
    schemas = schema["components"]["schemas"]
    assert "preference" in schemas["Parts"]["properties"] and "preference" in schemas["Parts"]["required"]
    assert "rating" in schemas["SuggestionOut"]["properties"] and "rating" in schemas["OutfitOut"]["properties"]
    assert "include" in schemas["SuggestIn"]["properties"]
    assert "must_include" in schemas["Problem"]["properties"]["cause"]["enum"]
    assert {"session", "messages", "tool_calls"} == set(schemas["SessionOut"]["properties"])
    assert {"outcome", "turn", "session"} == set(schemas["TurnOut"]["properties"])
    assert _query_names(schema, "/api/debug/llm-calls") == {"purpose", "errors_only", "limit", "session_id"}
    assert _query_names(schema, "/api/debug/tool-calls") == {"session_id", "limit"}
    assert _query_names(schema, "/api/wears") == {"item_id", "item_ids", "days"}
    assert _query_names(schema, "/api/weather") == {"place", "starts_at", "ends_at", "outdoors"}


def test_cli_openapi_keeps_the_items_query_parameters(data_dir, capsys):
    main(["openapi"])

    schema = json.loads(capsys.readouterr().out)
    assert _query_names(schema, "/api/items") == {
        "category",
        "color",
        "formality_min",
        "formality_max",
        "season",
        "status",
        "q",
        "not_worn_days",
    }


def test_the_committed_openapi_json_is_what_the_app_generates(data_dir, capsys):
    main(["openapi"])  # `npm run gen:api` writes this output to frontend/openapi.json

    committed = json.loads((FRONTEND_DIR / "openapi.json").read_text())
    assert json.loads(capsys.readouterr().out) == committed


def test_the_committed_frontend_types_name_every_phase_2_route_and_schema():
    types = (FRONTEND_DIR / "src" / "api" / "types.ts").read_text()  # generated by `npm run gen:api`

    for path, _method, _code in PHASE2_OPERATIONS:
        assert f'"{path}": {{' in types, path
    for name in ("SuggestOut", "SuggestionOut", "OutfitOut", "WearLogOut", "WeatherOut", "Problem", "Event", "WearIn"):
        assert f"        {name}: {{" in types, name


def test_the_committed_frontend_types_name_every_phase_3_route_and_schema():
    types = (FRONTEND_DIR / "src" / "api" / "types.ts").read_text()  # generated by `npm run gen:api`

    for path, _method, _code in PHASE3_OPERATIONS:
        assert f'"{path}": {{' in types, path
    for name in (
        "SessionSummary",
        "SessionOut",
        "TurnOut",
        "ProposalView",
        "CardSuggestion",
        "FeedbackIn",
        "WeatherReport",
    ):
        assert f"        {name}: {{" in types, name


def test_cli_openapi_starts_no_worker_thread(data_dir, capsys):
    main(["openapi"])

    assert not [t for t in threading.enumerate() if t.name == "kyapehnu-ingest" and t.is_alive()]


def test_version_flag(capsys):
    import kyapehnu
    from kyapehnu.core import cli

    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])

    assert e.value.code == 0 and capsys.readouterr().out.strip() == f"kyapehnu {kyapehnu.__version__}"


# --- the commands that need an account: registered, refusing an unknown one, never printing its email ---------------

EMAIL = "someone.private@example.com"


def test_restore_import_phase3_and_import_folder_are_registered(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    commands = re.search(r"\{([^}]*)\}", capsys.readouterr().out)
    assert exc.value.code == 0
    assert commands is not None
    assert {"restore", "import-phase3", "import-folder"} <= set(commands.group(1).split(","))


@pytest.mark.parametrize("argv", [["seed", "--email", EMAIL], ["import-folder", "photos", "--email", EMAIL]])
def test_a_command_for_an_unknown_account_refuses_and_never_echoes_the_email(data_dir, capsys, argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "No account matches" in captured.err
    assert EMAIL not in captured.out + captured.err
    assert _item_count(data_dir) == 0


def test_import_folder_runs_the_job_for_the_named_account_and_prints_ids_only(data_dir, capsys, tmp_path, monkeypatch):
    assert _account(data_dir) == 1
    monkeypatch.setattr("kyapehnu.core.cli.user_id_for_email", lambda _s, _email: 1)
    folder = tmp_path / "photos"
    folder.mkdir()
    for i in range(2):
        (folder / f"p{i}.jpg").write_bytes(b"x")  # not images: each photo fails on its own, no model call is made

    main(["import-folder", str(folder), "--email", EMAIL])

    out = capsys.readouterr()
    assert out.out == "import_done user_id=1 job=1 processed=2 failed=2\n"
    assert EMAIL not in out.out + out.err


def test_import_folder_refuses_a_folder_that_is_not_there(data_dir, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr("kyapehnu.core.cli.user_id_for_email", lambda _s, _email: 1)

    with pytest.raises(SystemExit) as exc:
        main(["import-folder", str(tmp_path / "nope"), "--email", EMAIL])

    assert exc.value.code == 2
    assert capsys.readouterr().err.strip()
