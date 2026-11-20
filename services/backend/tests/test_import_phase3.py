"""`kyapehnu import-phase3` (LLD 3.7; AC-29, AC-30, AC-45, AC-49): a Phase 3 data directory goes into one account, ids and
image bytes unchanged, the source untouched; every refusal and every failure leaves the stack as it was.

The image layout is the LLD rule `images/<user_id>/<kind>/<name>` written out here, not read from the wardrobe code
(that package changes in parallel). "Images open for A" is tested as: the file is at the path the row names, with the
source bytes, under A's folder only. The image route itself is tested in the wardrobe package."""

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from PIL import Image
from sqlmodel import Session

from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.core import import_phase3
from kyapehnu.core.cli import main
from kyapehnu.core.config import Prefs, get_settings
from kyapehnu.core.db import MIGRATIONS_DIR, OWNERSHIP_REVISION, make_engine, prepare_database, utcnow
from kyapehnu.user.public import ensure_profile

LAST_PHASE3 = "a7d2e4f39c60"
FIRST_PHASE3 = "971397b5ec4d"
EMAIL_A = "asha.private.import@example.com"
EMAIL_B = "bela.private.import@example.com"
SECRET_MARKER = "SECRETMARKER-4f1c9e"
NOW = "2026-09-01 10:00:00"
KINDS = ("original", "cutout", "thumb", "collage", "mannequin")
READ_ONLY_WORKS = os.name != "nt"

# source table -> key in the `import_done` line, in the order of LLD 3.6
COUNT_KEYS = {
    "item": "items",
    "photo": "photos",
    "ingestjob": "jobs",
    "ingestpiece": "pieces",
    "itemcorrection": "corrections",
    "event": "events",
    "outfit": "outfits",
    "wearlog": "wears",
    "chatsession": "chats",
    "chatmessage": "messages",
    "toolcall": "tool_calls",
    "llmcall": "llm_calls",
    "feedback": "feedback",
}
PATH_COLUMNS = {
    "photo": ("original_path", "normalized_path"),
    "item": ("cutout_path", "thumb_path", "alt_cutout_path", "alt_thumb_path"),
    "ingestpiece": ("cutout_path", "thumb_path", "alt_cutout_path", "alt_thumb_path"),
    "outfit": ("collage_path",),
}
OWNED_TABLES = [*COUNT_KEYS, "userprofile"]
IMAGE_FORMATS = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}


# ---- the Phase 3 source directory ------------------------------------------------------------------------------


@dataclass
class Source:
    root: Path
    revision: str
    files: dict[str, bytes] = field(default_factory=dict)  # image files that exist, by path relative to the root
    referenced: set[str] = field(default_factory=set)  # every path a row names, existing or not
    counts: dict[str, int] = field(default_factory=dict)
    last_id: int = 0


def _upgrade(root: Path, revision: str) -> None:
    engine = make_engine(root)
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)
    engine.dispose()


def _image_bytes(rel: str, serial: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (4, 4), (serial % 256, serial // 256 % 256, 99)).save(buf, IMAGE_FORMATS[rel.rsplit(".", 1)[1]])
    return buf.getvalue()


def _insert(con: sqlite3.Connection, table: str, **row: object) -> None:
    values = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in row.values()]
    con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", values)


def _build_source(root: Path, revision: str) -> Source:
    """A Phase 3 directory: a database migrated to `revision` with 32 items, their neighbours and the image files."""
    root.mkdir(parents=True)
    _upgrade(root, revision)
    last = revision == LAST_PHASE3
    src = Source(root=root, revision=revision)

    def image(rel: str, *, exists: bool = True) -> str:
        src.referenced.add(rel)
        if exists:
            data = _image_bytes(rel, len(src.files) + 1)
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)
            src.files[rel] = data
        return rel

    con = sqlite3.connect(root / "kyapehnu.db")
    for n in (1, 2):
        _insert(
            con,
            "ingestjob",
            id=n,
            status="done",
            source="upload",
            total=2,
            processed=2,
            failed=0,
            created_at=NOW,
            updated_at=NOW,
        )
    for n in (1, 2, 3):
        _insert(
            con,
            "photo",
            id=n,
            job_id=1 if n < 3 else 2,
            original_path=image(f"images/original/ph{n}.jpg"),
            normalized_path=image(f"images/original/ph{n}_norm.jpg"),
            phash="ab12",
            photo_type="flat_lay",
            status="done",
            created_at=NOW,
        )
    for n in range(1, 33):
        extra = {}
        if last and n == 1:
            extra = {
                "alt_cutout_path": image("images/cutout/it1_alt.png"),
                "alt_thumb_path": image("images/thumb/it1_alt.webp"),
                "picture_generated": 1,
            }
        _insert(
            con,
            "item",
            id=n,
            photo_id=1 if n <= 3 else None,
            name=f"Item {n}",
            category="top",
            subcategory="tee",
            colors=["navy"],
            pattern="solid",
            formality=3,
            warmth=2,
            seasons=["summer"],
            status="available",
            cutout_path=image(f"images/cutout/it{n}.png"),
            thumb_path=image(f"images/thumb/it{n}.webp", exists=n != 32),  # one file is gone: missing_images=1
            tags=["cotton"],
            notes=f"note {n}",
            created_at=NOW,
            updated_at=NOW,
            **extra,
        )
    for n in (1, 2):
        _insert(
            con,
            "ingestpiece",
            id=n,
            job_id=1,
            photo_id=1,
            bbox=[0, 0, 1, 1],
            cutout_path=image(f"images/cutout/pc{n}.png"),
            thumb_path=image(f"images/thumb/pc{n}.webp"),
            state="accepted",
            item_id=n,
            created_at=NOW,
        )
        _insert(con, "itemcorrection", id=n, item_id=1, field="name", ai_value='"a"', user_value='"b"', created_at=NOW)
    for n in (1, 2, 3):
        _insert(
            con,
            "llmcall",
            id=n,
            provider="anthropic",
            model="m",
            purpose="tagging",
            request_summary="s",
            latency_ms=5,
            created_at=NOW,
        )
    profile = {
        "id": 1,
        "body_notes": "bn",
        "skin_tone": "warm",
        "style_prefs": "classic",
        "colors_loved": ["navy"],
        "colors_avoided": ["red"],
        "fit_prefs": "slim",
        "budget_min": 1000,
        "budget_max": 5000,
        "home_city": "Pune",
    }
    if last:
        profile |= {"height_cm": 170, "weight_kg": 65, "gender": "f", "name": "Asha", "age": 30}
    _insert(con, "userprofile", **profile)
    if last:
        _insert_phase3_neighbours(con, image)
        con.execute("UPDATE itemseq SET last_id = 40 WHERE id = 1")
        for rel in ("images/mannequin/1-2-3.png", "images/mannequin/4-5.png"):  # named by item ids, referenced by none
            image(rel)
            src.referenced.discard(rel)
    src.last_id = 40 if last else 32  # the upgrade of a Phase 1 database seeds the counter with the largest item id
    con.commit()
    present = {name for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    src.counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] if t in present else 0 for t in COUNT_KEYS}
    con.close()
    (root / "secrets.json").write_text(json.dumps({"anthropic_api_key": SECRET_MARKER}))
    (root / "config.yaml").write_text(f"home_city: {SECRET_MARKER}\n")
    return src


def _insert_phase3_neighbours(con: sqlite3.Connection, image: Callable[..., str]) -> None:
    for n in (1, 2):
        _insert(
            con,
            "event",
            id=n,
            title=f"Event {n}",
            type="social",
            starts_at=NOW,
            place="Pune",
            outdoors=0,
            dress_code="casual",
            audience_tags=[],
            notes="",
            created_at=NOW,
        )
        _insert(
            con,
            "outfit",
            id=n,
            item_ids=[n, n + 1],
            item_key=f"{n}-{n + 1}",
            name=f"Outfit {n}",
            created_by="user",
            occasion_tags=[],
            collage_path=image(f"images/collage/of{n}.png"),
            created_at=NOW,
        )
        _insert(con, "feedback", id=n, outfit_id=n, rating=1 if n == 1 else -1, reason="", created_at=NOW)
    for n in (1, 2, 3):
        _insert(
            con,
            "wearlog",
            id=n,
            outfit_id=1,
            item_ids=[1, 2],
            worn_on="2026-09-01",
            event_id=1,
            audience_tags=[],
            created_at=NOW,
        )
    for chat in (1, 5):  # ids are never reused: the gap must survive the import
        _insert(con, "chatsession", id=chat, title=f"Chat {chat}", created_at=NOW, updated_at=NOW)
    for n, chat in enumerate((1, 1, 5, 5), start=1):
        _insert(
            con,
            "chatmessage",
            id=n,
            session_id=chat,
            turn=1,
            step=0,
            role="user" if n % 2 else "assistant",
            content=f"message {n}",
            created_at=NOW,
        )
    for n in (1, 2):
        _insert(
            con,
            "toolcall",
            id=n,
            session_id=1,
            turn=1,
            step=n,
            call_id=f"call-{n}",
            name="list_items",
            args={},
            result="ok",
            truncated=0,
            latency_ms=3,
            created_at=NOW,
        )


def _set_mode(root: Path, *, read_only: bool) -> None:
    if not READ_ONLY_WORKS:
        return
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            os.chmod(Path(dirpath) / name, 0o444 if read_only else 0o644)
        os.chmod(dirpath, 0o555 if read_only else 0o755)


def edit_db(root: Path, sql: str, params: tuple = ()) -> None:
    con = sqlite3.connect(root / "kyapehnu.db")
    con.execute(sql, params)
    con.commit()
    con.close()


@pytest.fixture
def make_source(tmp_path):
    """Build a Phase 3 directory, let the test tamper with it, then make it read-only like `-v DIR:/import:ro`."""
    made: list[Path] = []

    def _make(revision: str = LAST_PHASE3, *, tamper: Callable[[Path], None] | None = None) -> Source:
        root = tmp_path / f"phase3-{len(made)}"
        src = _build_source(root, revision)
        if tamper is not None:
            tamper(root)
        made.append(root)
        _set_mode(root, read_only=True)
        return src

    yield _make
    for root in made:
        _set_mode(root, read_only=False)


def tree_digest(root: Path) -> tuple[dict[str, str], list[str]]:
    files = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
    dirs = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir())
    return files, dirs


# ---- the stack the data goes into ------------------------------------------------------------------------------


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """An empty stack with two accounts. B signed in first (id 1, the admin), A second (id 2): a user id hard-coded
    to 1 or left at a default lands in the wrong account."""
    data_dir = tmp_path / "stack"
    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KYAPEHNU_CONFIG_FILE", str(tmp_path / "config.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    engine = prepare_database(data_dir)
    ids = {}
    with Session(engine) as s:
        for key, email in (("b", EMAIL_B), ("a", EMAIL_A)):
            user = upsert_google_user(s, sub=f"sub-{key}", email=email, name=key.upper(), now=utcnow())
            ensure_profile(user.id, s, Prefs())
            ids[key] = user.id
    engine.dispose()
    sql(data_dir, "UPDATE userprofile SET home_city = 'Bergen' WHERE user_id = ?", (ids["b"],))
    yield SimpleNamespace(data_dir=data_dir, a=ids["a"], b=ids["b"])
    get_settings.cache_clear()


def sql(data_dir: Path, statement: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(data_dir / "kyapehnu.db")
    try:
        rows = con.execute(statement, params).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def stack_state(stack) -> dict:
    """Everything an import may change: every owned row, the counters, the account rows and the files besides the
    database itself."""
    con = sqlite3.connect(stack.data_dir / "kyapehnu.db")
    try:
        tables = [*OWNED_TABLES, '"user"', "itemseq", "sqlite_sequence"]
        state: dict = {t: con.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}
    finally:
        con.close()
    state["files"] = sorted(
        p.relative_to(stack.data_dir).as_posix()
        for p in stack.data_dir.rglob("*")
        if p.is_file() and not p.name.startswith("kyapehnu.db")
    )
    return state


def images_in(stack) -> list[Path]:
    return sorted(p for p in (stack.data_dir / "images").rglob("*") if p.is_file())


def run_import(source: Source, email: str = EMAIL_A) -> None:
    main(["import-phase3", "--source", str(source.root), "--email", email])


def parse_done(out: str) -> dict[str, int]:
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, out
    match = re.fullmatch(r"import_done user_id=(\d+)( .*)", lines[0])
    assert match, lines[0]
    pairs = dict(item.split("=") for item in lines[0].split()[1:])
    return {k: int(v) for k, v in pairs.items()}


def refuse(source: Source, capsys, email: str = EMAIL_A) -> str:
    """The import stops with exit code 2 before changing anything. Returns what it printed on stderr."""
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        run_import(source, email)
    out = capsys.readouterr()
    assert exc.value.code == 2
    assert out.out == ""
    assert out.err.strip()
    assert EMAIL_A not in out.err and EMAIL_B not in out.err
    return out.err


def assert_still_importable(stack, make_source, capsys) -> None:
    """The control of every refusal: a clean source still imports into the same account, so the refused run left no
    row, counter, file or lock behind that would block it."""
    good = make_source()
    capsys.readouterr()
    run_import(good)
    done = parse_done(capsys.readouterr().out)
    assert done["user_id"] == stack.a
    assert done["items"] == 32


# ---- AC-29 / AC-45: the import ---------------------------------------------------------------------------------


@pytest.fixture(params=[LAST_PHASE3, FIRST_PHASE3], ids=["at-a7d2e4f39c60", "at-971397b5ec4d"])
def imported(request, stack, make_source, capsys, caplog):
    """The source at the last Phase 3 revision and at the first, imported into A."""
    source = make_source(request.param)
    before = SimpleNamespace(
        source=tree_digest(source.root),
        profile_b=sql(stack.data_dir, "SELECT * FROM userprofile WHERE user_id = ?", (stack.b,)),
        users=sql(stack.data_dir, 'SELECT * FROM "user" ORDER BY id'),
    )
    caplog.set_level(logging.DEBUG)
    capsys.readouterr()
    try:
        run_import(source)
    except SystemExit as e:  # a BaseException would break the fixture teardown; report it as a plain failure
        pytest.fail(f"the import exited with code {e.code}: {capsys.readouterr().err.strip()}")
    out = capsys.readouterr()
    return SimpleNamespace(source=source, stack=stack, before=before, out=out.out, err=out.err, log=caplog.text)


def test_the_import_prints_one_done_line_with_the_user_id_and_the_counts(imported):
    done = parse_done(imported.out)

    expected = {"user_id": imported.stack.a} | {key: imported.source.counts[t] for t, key in COUNT_KEYS.items()}
    expected["images"] = len(imported.source.files)
    expected["missing_images"] = 1
    assert list(done) == ["user_id", *COUNT_KEYS.values(), "images", "missing_images"]
    assert done == expected


def test_the_email_is_in_no_output_and_the_log_has_the_done_event_with_the_user_id(imported):
    for text in (imported.out, imported.err, imported.log):
        assert EMAIL_A not in text and EMAIL_B not in text
    assert f"event=import_done user_id={imported.stack.a}" in imported.log


def test_every_source_row_is_in_the_target_for_A_with_its_id_unchanged(imported):
    con = sqlite3.connect(f"{imported.source.root.joinpath('kyapehnu.db').as_uri()}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    a = imported.stack.a
    try:
        for table in COUNT_KEYS:
            types = {r["name"]: r["type"] for r in con.execute(f"PRAGMA table_info({table})")}
            source_rows = {r["id"]: dict(r) for r in con.execute(f"SELECT * FROM {table}")} if types else {}
            target = sql(imported.stack.data_dir, f"SELECT * FROM {table} WHERE user_id = ?", (a,))
            columns = [r[1] for r in sql(imported.stack.data_dir, f"PRAGMA table_info({table})")]
            target_rows = {r[columns.index("id")]: dict(zip(columns, r, strict=True)) for r in target}
            assert set(target_rows) == set(source_rows), table
            for row_id, row in source_rows.items():
                for column, value in row.items():
                    if column in PATH_COLUMNS.get(table, ()):
                        continue
                    assert _norm(types[column], target_rows[row_id][column]) == _norm(types[column], value), (
                        table,
                        row_id,
                        column,
                    )
    finally:
        con.close()


def _norm(declared: str, value: object) -> object:
    """Compare values, not their text: the importer may store a JSON or a time in another spelling."""
    if value is None:
        return None
    if declared.startswith("JSON"):
        return json.loads(value) if isinstance(value, str) else value
    if declared == "DATETIME":
        return datetime.fromisoformat(str(value))
    if declared == "DATE":
        return date.fromisoformat(str(value))
    return value


def test_B_owns_nothing_and_the_account_rows_and_B_profile_are_unchanged(imported):
    stack = imported.stack
    for table in COUNT_KEYS:
        assert sql(stack.data_dir, f"SELECT count(*) FROM {table} WHERE user_id = ?", (stack.b,)) == [(0,)], table
        assert sql(stack.data_dir, f"SELECT count(*) FROM {table}")[0][0] == imported.source.counts[table], table
    assert sql(stack.data_dir, "SELECT * FROM userprofile WHERE user_id = ?", (stack.b,)) == imported.before.profile_b
    assert sql(stack.data_dir, 'SELECT * FROM "user" ORDER BY id') == imported.before.users


def test_the_profile_of_A_takes_the_fields_of_the_source_profile(imported):
    stack = imported.stack
    fields = (
        "body_notes, skin_tone, style_prefs, colors_loved, colors_avoided, fit_prefs, budget_min, budget_max, home_city"
    )
    if imported.source.revision == LAST_PHASE3:
        fields += ", height_cm, weight_kg, gender, name, age"

    target = sql(stack.data_dir, f"SELECT {fields} FROM userprofile WHERE user_id = ?", (stack.a,))
    source = sqlite3.connect(f"{imported.source.root.joinpath('kyapehnu.db').as_uri()}?mode=ro&immutable=1", uri=True)
    expected = source.execute(f"SELECT {fields} FROM userprofile WHERE id = 1").fetchall()
    source.close()

    def values(rows: list[tuple]) -> list[tuple]:
        return [tuple(json.loads(v) if isinstance(v, str) and v.startswith("[") else v for v in row) for row in rows]

    assert values(target) == values(expected)
    assert sql(stack.data_dir, "SELECT home_city FROM userprofile WHERE user_id = ?", (stack.a,)) == [("Pune",)]
    assert sql(stack.data_dir, "SELECT count(*) FROM userprofile") == [(2,)]


def test_every_image_path_names_a_file_under_the_folder_of_A_with_the_source_bytes(imported):
    stack = imported.stack
    layout = re.compile(rf"images/{stack.a}/({'|'.join(KINDS)})/[A-Za-z0-9_-]{{1,200}}\.(jpg|jpeg|png|webp|heic|heif)")
    checked = 0
    for table, columns in PATH_COLUMNS.items():
        for column in columns:
            for new, old in zip(
                sql(stack.data_dir, f"SELECT {column} FROM {table} ORDER BY id"),
                _source_column(imported.source, table, column),
                strict=True,
            ):
                if old is None:
                    assert new == (None,)
                    continue
                assert layout.fullmatch(new[0]), new
                kind, name = old.split("/")[1:]
                assert new[0] == f"images/{stack.a}/{kind}/{name}"
                if old in imported.source.files:
                    assert (stack.data_dir / new[0]).read_bytes() == imported.source.files[old]
                    checked += 1
    assert checked > 60


def _source_column(source: Source, table: str, column: str) -> list[object]:
    con = sqlite3.connect(f"{source.root.joinpath('kyapehnu.db').as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        return [r[0] for r in con.execute(f"SELECT {column} FROM {table} ORDER BY id")]
    except sqlite3.OperationalError:
        return [None] * source.counts[table]  # the column is newer than this source revision
    finally:
        con.close()


def test_every_image_file_of_the_stack_is_under_the_folder_of_A_and_B_has_none(imported):
    stack = imported.stack

    files = images_in(stack)

    assert len(files) == len(imported.source.files)
    assert all(p.relative_to(stack.data_dir / "images").parts[0] == str(stack.a) for p in files)
    assert not (stack.data_dir / "images" / str(stack.b)).exists() or not list(
        (stack.data_dir / "images" / str(stack.b)).rglob("*.*")
    )


def test_the_mannequin_pictures_are_copied_with_their_names(imported):
    if imported.source.revision != LAST_PHASE3:
        pytest.skip("Phase 1 databases have no mannequin pictures")
    for rel in ("images/mannequin/1-2-3.png", "images/mannequin/4-5.png"):
        copy = imported.stack.data_dir / "images" / str(imported.stack.a) / "mannequin" / rel.rsplit("/", 1)[1]
        assert copy.read_bytes() == imported.source.files[rel]


def test_the_item_counter_is_the_largest_of_the_source_counter_and_the_item_ids(imported):
    assert sql(imported.stack.data_dir, "SELECT last_id FROM itemseq") == [(imported.source.last_id,)]


def test_the_chat_id_gap_survives_so_a_new_chat_gets_a_fresh_id(imported):
    if imported.source.revision != LAST_PHASE3:
        pytest.skip("Phase 1 databases have no chats")
    stack = imported.stack
    assert sorted(r[0] for r in sql(stack.data_dir, "SELECT id FROM chatsession")) == [1, 5]

    sql(
        stack.data_dir,
        "INSERT INTO chatsession (title, created_at, updated_at, user_id) VALUES ('n', ?, ?, ?)",
        (NOW, NOW, stack.b),
    )

    assert sql(stack.data_dir, "SELECT max(id) FROM chatsession") == [(6,)]


def test_the_source_directory_is_byte_identical_and_gains_no_file(imported):
    assert tree_digest(imported.source.root) == imported.before.source


def test_secrets_and_config_of_the_source_are_not_imported(imported):
    stack = imported.stack
    assert not (stack.data_dir / "secrets.json").exists()
    assert not (stack.data_dir / "config.yaml").exists()
    for p in stack.data_dir.rglob("*"):
        if p.is_file():
            assert SECRET_MARKER.encode() not in p.read_bytes(), p


def test_the_imported_database_passes_the_startup_rule(imported):
    engine = prepare_database(imported.stack.data_dir)  # the startup rule accepts the imported database
    engine.dispose()


def test_a_source_without_an_images_folder_imports_and_counts_every_file_as_missing(stack, make_source, capsys):
    source = make_source(tamper=lambda root: shutil.rmtree(root / "images"))
    capsys.readouterr()

    run_import(source)

    done = parse_done(capsys.readouterr().out)
    assert done["images"] == 0 and done["missing_images"] == len(source.referenced)
    assert done["items"] == 32
    assert images_in(stack) == []


# ---- AC-30: refusals leave nothing behind ----------------------------------------------------------------------


def test_a_second_run_for_the_same_account_is_refused_and_changes_nothing(stack, make_source, capsys):
    run_import(make_source())
    after_first = stack_state(stack)
    capsys.readouterr()

    err = refuse(make_source(), capsys)

    assert "already imported or account in use" in err
    assert stack_state(stack) == after_first


def _insert_target_item(stack, owner: int, item_id: int) -> None:
    sql(
        stack.data_dir,
        "INSERT INTO item (id, name, category, subcategory, colors, pattern, formality, warmth, seasons, status, tags,"
        " notes, created_at, updated_at, user_id) VALUES (?, 'x', 'top', 't', '[]', 'solid', 3, 3, '[]', 'available',"
        " '[]', '', ?, ?, ?)",
        (item_id, NOW, NOW, owner),
    )


# name -> (block, unblock): what makes preflight refuse, and the one change that lets the same source through
PREFLIGHT_CASES = {
    "A already owns an item": (
        lambda st: _insert_target_item(st, st.a, 900),
        lambda st: sql(st.data_dir, "DELETE FROM item WHERE id = 900"),
    ),
    "a source item id is already taken by another account": (
        lambda st: _insert_target_item(st, st.b, 7),
        lambda st: sql(st.data_dir, "DELETE FROM item WHERE id = 7"),
    ),
    "the item counter of the stack is past a source item id": (
        lambda st: sql(st.data_dir, "UPDATE itemseq SET last_id = 100"),
        lambda st: sql(st.data_dir, "UPDATE itemseq SET last_id = 0"),
    ),
    "a chat id of the source was used before on the stack": (
        lambda st: (
            sql(
                st.data_dir,
                "INSERT INTO chatsession (id, title, created_at, updated_at, user_id) VALUES (1,'c',?,?,?)",
                (NOW, NOW, st.b),
            ),
            sql(st.data_dir, "DELETE FROM chatsession WHERE id = 1"),  # the id is gone, the sequence remembers it
        ),
        lambda st: sql(st.data_dir, "UPDATE sqlite_sequence SET seq = 0 WHERE name = 'chatsession'"),
    ),
}


@pytest.mark.parametrize("case", PREFLIGHT_CASES)
def test_a_preflight_refusal_writes_nothing_and_the_same_source_imports_once_it_is_cleared(
    stack, make_source, capsys, case
):
    block, unblock = PREFLIGHT_CASES[case]
    block(stack)
    source = make_source()
    before, digest = stack_state(stack), tree_digest(source.root)

    refuse(source, capsys)

    assert stack_state(stack) == before
    assert tree_digest(source.root) == digest
    unblock(stack)
    run_import(source)
    assert parse_done(capsys.readouterr().out)["items"] == 32


@pytest.mark.parametrize("stamp", ["deadbeef0000", OWNERSHIP_REVISION], ids=["foreign-revision", "ownership-revision"])
def test_a_source_at_an_unknown_revision_is_refused_and_nothing_is_written(stack, make_source, capsys, stamp):
    source = make_source(tamper=lambda root: edit_db(root, "UPDATE alembic_version SET version_num = ?", (stamp,)))
    before, digest = stack_state(stack), tree_digest(source.root)

    refuse(source, capsys)

    assert stack_state(stack) == before
    assert tree_digest(source.root) == digest
    assert_still_importable(stack, make_source, capsys)


def test_a_source_without_a_database_is_refused(stack, make_source, capsys):
    source = make_source(tamper=lambda root: (root / "kyapehnu.db").unlink())
    before = stack_state(stack)

    refuse(source, capsys)

    assert stack_state(stack) == before
    assert_still_importable(stack, make_source, capsys)


def test_a_source_database_that_is_not_sqlite_is_refused(stack, make_source, capsys):
    source = make_source(tamper=lambda root: (root / "kyapehnu.db").write_bytes(os.urandom(4096)))
    before = stack_state(stack)

    refuse(source, capsys)

    assert stack_state(stack) == before
    assert_still_importable(stack, make_source, capsys)


@pytest.mark.parametrize("who", ["nobody@example.com", EMAIL_B + "|twin"], ids=["no-account", "two-accounts"])
def test_an_email_without_exactly_one_account_is_refused_and_never_printed(stack, make_source, capsys, who):
    if who.endswith("|twin"):
        who = who.removesuffix("|twin")
        engine = prepare_database(stack.data_dir)
        with Session(engine) as s:
            upsert_google_user(s, sub="sub-twin", email=who, name="Twin", now=utcnow())
        engine.dispose()
    source = make_source()
    before = stack_state(stack)

    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        run_import(source, who)
    out = capsys.readouterr()

    assert exc.value.code == 2
    assert who not in out.out + out.err
    assert stack_state(stack) == before
    run_import(source)  # the account A is still importable
    assert parse_done(capsys.readouterr().out)["user_id"] == stack.a


# ---- crafted databases -----------------------------------------------------------------------------------------

CRAFTED_PATHS = [
    "images/cutout/../../secrets.json",
    "../secrets.json",
    "/etc/passwd",
    "images/cutout/sub/a.png",
    "images/cutout/a.png/../../../secrets.json",
    "images/other/a.png",
    "images/cutout/a.txt",
    "images/cutout/" + "a" * 201 + ".png",
    "images\\cutout\\a.png",
    "images/cutout/.png",
    "images/cutout/a b.png",
    "images/cutout/..",
]
PATH_CASES = [(t, c) for t, cols in PATH_COLUMNS.items() for c in cols]


def _assert_crafted_source_refused(stack, make_source, capsys, table: str, column: str, value: str) -> None:
    source = make_source(tamper=lambda root: edit_db(root, f"UPDATE {table} SET {column} = ? WHERE id = 1", (value,)))
    before, digest = stack_state(stack), tree_digest(source.root)

    refuse(source, capsys)

    assert stack_state(stack) == before
    assert tree_digest(source.root) == digest
    for p in stack.data_dir.rglob("*"):
        if p.is_file() and p.name != "kyapehnu.db":
            assert SECRET_MARKER.encode() not in p.read_bytes(), p
    assert_still_importable(stack, make_source, capsys)


@pytest.mark.parametrize("value", CRAFTED_PATHS)
def test_a_crafted_image_path_is_refused_and_nothing_is_written(stack, make_source, capsys, value):
    _assert_crafted_source_refused(stack, make_source, capsys, "item", "cutout_path", value)


@pytest.mark.parametrize(("table", "column"), PATH_CASES)
def test_every_path_column_is_checked(stack, make_source, capsys, table, column):
    _assert_crafted_source_refused(stack, make_source, capsys, table, column, "images/cutout/../../secrets.json")


# ---- references that leave the source (review M-1) -------------------------------------------------------------

B_ITEM_ID = 900  # an item of B's, above every id of the source


@pytest.mark.parametrize(
    "tamper_sql",
    [
        f"UPDATE itemcorrection SET item_id = {B_ITEM_ID} WHERE id = 1",
        f"UPDATE ingestpiece SET duplicate_of_item_id = {B_ITEM_ID} WHERE id = 1",
        f"UPDATE outfit SET item_ids = '[1, {B_ITEM_ID}]' WHERE id = 1",
        f"UPDATE wearlog SET item_ids = '[{B_ITEM_ID}]' WHERE id = 1",
        "UPDATE wearlog SET outfit_id = 999 WHERE id = 1",
        "UPDATE chatmessage SET session_id = 999 WHERE id = 1",
        "UPDATE outfit SET item_ids = 'not json' WHERE id = 1",
    ],
    ids=["correction", "piece-duplicate", "outfit-list", "wearlog-list", "wearlog-outfit", "message-chat", "bad-list"],
)
def test_a_row_that_points_outside_the_source_is_refused_and_nothing_is_written(stack, make_source, capsys, tamper_sql):
    _insert_target_item(stack, stack.b, B_ITEM_ID)
    source = make_source(tamper=lambda root: edit_db(root, tamper_sql))
    before, digest = stack_state(stack), tree_digest(source.root)

    err = refuse(source, capsys)

    assert "source" in err
    assert stack_state(stack) == before
    assert images_in(stack) == []
    assert tree_digest(source.root) == digest
    assert_still_importable(stack, make_source, capsys)


def test_an_item_list_naming_an_item_deleted_in_the_source_still_imports(stack, make_source, capsys):
    source = make_source(tamper=lambda root: edit_db(root, "UPDATE outfit SET item_ids = '[1, 4242]' WHERE id = 1"))

    run_import(source)

    assert parse_done(capsys.readouterr().out)["outfits"] == source.counts["outfit"]
    assert sql(stack.data_dir, "SELECT item_ids FROM outfit WHERE id = 1") == [("[1, 4242]",)]


# ---- failure inside the transaction ----------------------------------------------------------------------------


def test_a_failure_while_writing_rolls_back_the_rows_and_removes_the_copied_images_and_their_folders(
    stack, make_source, capsys, monkeypatch
):
    insert = import_phase3._insert_table

    def fail_on_the_last_table(con, table, user_id):
        if table == "feedback":
            raise sqlite3.OperationalError("disk I/O error")
        return insert(con, table, user_id)

    monkeypatch.setattr(import_phase3, "_insert_table", fail_on_the_last_table)
    source = make_source()
    before = stack_state(stack)

    with pytest.raises(sqlite3.OperationalError):
        run_import(source)

    assert stack_state(stack) == before
    assert images_in(stack) == []
    assert list((stack.data_dir / "images").iterdir()) == []  # not even the empty folders of A
    monkeypatch.undo()
    capsys.readouterr()
    assert_still_importable(stack, make_source, capsys)


# ---- a link in the source must not read the host (review H-1) --------------------------------------------------

CROSS_TENANT = b"CROSS-TENANT-MARKER-7b21"


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not supported here")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(
    ("linked", "prefix"),
    [("images", "images/"), ("kind-folder", "images/thumb/"), ("file", "images/thumb/it1.webp")],
)
def test_a_link_out_of_the_source_copies_nothing_and_counts_the_files_as_missing(
    stack, make_source, capsys, tmp_path, linked, prefix
):
    """The link stands in for `/data/images/<B>` of the stack: its files carry the names the rows of the source name."""
    outside = tmp_path / "data" / "images" / "2"

    def tamper(root: Path) -> None:
        moved = outside / prefix.removeprefix("images/")  # where the files go: outside, with the marker as content
        moved.parent.mkdir(parents=True)
        shutil.move(root / prefix.removesuffix("/"), moved)
        for p in [moved] if moved.is_file() else moved.rglob("*"):
            if p.is_file():
                p.write_bytes(CROSS_TENANT)
        _link(root / prefix.removesuffix("/"), moved)

    source = make_source(tamper=tamper)
    cut = {rel for rel in source.files if rel.startswith(prefix)}
    assert cut  # the link hides real files
    expected_missing = len(source.referenced - (set(source.files) - cut))
    before = SimpleNamespace(source=tree_digest(source.root), outside=tree_digest(outside))
    capsys.readouterr()

    run_import(source)

    done = parse_done(capsys.readouterr().out)
    assert done["images"] == len(source.files) - len(cut)
    assert done["missing_images"] == expected_missing
    assert done["items"] == 32
    own = stack.data_dir / "images" / str(stack.a)
    assert not [p for p in own.rglob("*") if p.is_file() and CROSS_TENANT in p.read_bytes()]
    assert tree_digest(source.root) == before.source
    assert tree_digest(outside) == before.outside


# ---- rows committed only in the write-ahead log of the source (review M-2) -------------------------------------


def test_rows_committed_only_in_the_wal_of_the_source_are_imported(stack, make_source, capsys):
    def tamper(root: Path) -> None:
        db = root / "kyapehnu.db"
        wal = root / "kyapehnu.db-wal"
        con = sqlite3.connect(db)
        try:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA wal_autocheckpoint = 0")
            _insert(
                con,
                "ingestjob",
                id=99,
                status="done",
                source="upload",
                total=1,
                processed=1,
                failed=0,
                created_at=NOW,
                updated_at=NOW,
            )
            con.commit()
            # an unclean shutdown: the database file without the row and the log with it
            saved_db, saved_wal = db.read_bytes(), wal.read_bytes()
        finally:
            con.close()  # the last connection checkpoints and deletes the log
        db.write_bytes(saved_db)
        wal.write_bytes(saved_wal)
        (root / "kyapehnu.db-shm").unlink(missing_ok=True)
        with closing(sqlite3.connect(f"{db.as_uri()}?immutable=1", uri=True)) as ro:  # the file alone lacks the row
            assert ro.execute("SELECT count(*) FROM ingestjob WHERE id = 99").fetchone() == (0,)

    source = make_source(tamper=tamper)
    before = tree_digest(source.root)
    assert "kyapehnu.db-wal" in before[0]
    capsys.readouterr()

    run_import(source)

    done = parse_done(capsys.readouterr().out)
    assert done["jobs"] == source.counts["ingestjob"] + 1
    assert sql(stack.data_dir, "SELECT user_id FROM ingestjob WHERE id = 99") == [(stack.a,)]
    assert tree_digest(source.root) == before
