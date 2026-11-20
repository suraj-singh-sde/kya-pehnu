"""`kyapehnu backup` and `kyapehnu restore` (LLD 3.6; AC-31, AC-32): two users' data goes into an archive and comes back,
the archive holds no secret, and a restore accepts nothing but a database of this version and image files with the names
the image route serves (no traversal), copies the images first and puts the database in place last."""

import hashlib
import os
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session

from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.auth.public import user_id_for_email
from kyapehnu.auth.sessions import SessionStore
from kyapehnu.core.backup import backup
from kyapehnu.core.cli import main
from kyapehnu.core.config import Prefs, get_settings
from kyapehnu.core.db import HEAD_REVISION, MIGRATIONS_DIR, make_engine, prepare_database, utcnow
from kyapehnu.files.local import LocalStore
from kyapehnu.user.public import ensure_profile
from tests.fake_google import CLIENT_SECRET

NOW = "2026-09-01 10:00:00"
EMAILS = {"a": "asha.private.backup@example.com", "b": "bela.private.backup@example.com"}
LLM_KEY_MARKER = "LLM-KEY-MARKER-77d2"
GOOGLE_TOKEN_MARKER = "GOOGLE-TOKEN-MARKER-31ab"
ARCHIVE_MEMBER = re.compile(
    r"kyapehnu\.db|images/\d+/(original|cutout|thumb|collage|mannequin)/[A-Za-z0-9_-]{1,200}\.\w+"
)
ITEMS = {"a": 3, "b": 2}
RANDOM_BYTES = 4096


def sql(data_dir: Path, statement: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(data_dir / "kyapehnu.db")
    try:
        rows = con.execute(statement, params).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def _env(monkeypatch, data_dir: Path) -> None:
    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KYAPEHNU_CONFIG_FILE", str(data_dir.parent / "config.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def cli_restore(monkeypatch):
    """`kyapehnu restore ARCHIVE` against the stack in `data_dir`."""

    def _restore(data_dir: Path, archive: Path) -> None:
        _env(monkeypatch, data_dir)
        main(["restore", str(archive)])

    yield _restore
    get_settings.cache_clear()


def _write(data_dir: Path, files: dict[str, bytes], rel: str) -> None:
    files[rel] = f"image {len(files)} of {rel}".encode() + os.urandom(16)
    (data_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (data_dir / rel).write_bytes(files[rel])


@pytest.fixture
def source(tmp_path, monkeypatch, kv):
    """A stack that has been used by two people: A (id 1, the admin) and B (id 2), each with items, images and a live
    session, plus the secrets a real data directory holds (a key file and a config file)."""
    data_dir = tmp_path / "stack"
    _env(monkeypatch, data_dir)
    engine = prepare_database(data_dir)
    ids, sessions = {}, []
    with Session(engine) as s:
        for key in ("a", "b"):
            user = upsert_google_user(s, sub=f"sub-{key}", email=EMAILS[key], name=key.upper(), now=utcnow())
            ensure_profile(user.id, s, Prefs())
            ids[key] = user.id
            sessions.append(SessionStore(kv, 3600).create(user.id))
    engine.dispose()
    files: dict[str, bytes] = {}
    for key, uid in ids.items():
        sql(data_dir, "UPDATE userprofile SET home_city = ? WHERE user_id = ?", (f"City of {key}", uid))
        for n in range(1, ITEMS[key] + 1):
            paths = {kind: f"images/{uid}/{kind}/{key}{n}.png" for kind in ("cutout", "thumb")}
            for rel in paths.values():
                _write(data_dir, files, rel)
            sql(
                data_dir,
                "INSERT INTO item (name, category, subcategory, colors, pattern, formality, warmth, seasons, status,"
                " cutout_path, thumb_path, tags, notes, created_at, updated_at, user_id)"
                " VALUES (?, 'top', 't', '[]', 'solid', 3, 3, '[]', 'available', ?, ?, '[]', ?, ?, ?, ?)",
                (f"{key} item {n}", paths["cutout"], paths["thumb"], f"note {key}{n}", NOW, NOW, uid),
            )
        _write(data_dir, files, f"images/{uid}/mannequin/{uid}-{uid + 1}.png")
    (data_dir / "secrets.json").write_text(
        f'{{"anthropic_api_key": "{LLM_KEY_MARKER}", "token": "{GOOGLE_TOKEN_MARKER}"}}'
    )
    (data_dir / "config.yaml").write_text(f"home_city: {LLM_KEY_MARKER}\n")
    (data_dir / "backups").mkdir()
    (data_dir / "backups" / "kyapehnu-old.zip").write_bytes(b"PK older backup")
    return SimpleNamespace(data_dir=data_dir, a=ids["a"], b=ids["b"], files=files, sessions=sessions)


@pytest.fixture
def archive(source, tmp_path) -> Path:
    """A backup of the stack, copied out of its data directory (a wipe takes the `backups` folder with it)."""
    saved = tmp_path / "saved-backup.zip"
    shutil.copy(backup(source.data_dir, LocalStore(source.data_dir)), saved)
    return saved


def members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in z.namelist()}


def write_archive(path: Path, contents: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in contents.items():
            z.writestr(name, data)
    return path


def phase3_database(tmp_path: Path, revision: str) -> bytes:
    root = tmp_path / f"db-{revision}"
    engine = make_engine(root)
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)
    engine.dispose()
    return (root / "kyapehnu.db").read_bytes()


def stamped_database(tmp_path: Path, revision: str) -> bytes:
    root = tmp_path / "db-stamped"
    prepare_database(root).dispose()
    sql(root, "UPDATE alembic_version SET version_num = ?", (revision,))
    return (root / "kyapehnu.db").read_bytes()


# ---- AC-32: what the archive holds -----------------------------------------------------------------------------


def test_the_archive_holds_the_database_and_the_images_of_both_users_and_nothing_else(source, archive):
    contents = members(archive)

    assert set(contents) == {"kyapehnu.db", *source.files}
    assert all(ARCHIVE_MEMBER.fullmatch(name) for name in contents)
    assert all(contents[name] == data for name, data in source.files.items())


def test_the_archive_carries_no_key_client_secret_token_or_session_value(source, archive, tmp_path):
    contents = members(archive)
    forbidden = [CLIENT_SECRET, LLM_KEY_MARKER, GOOGLE_TOKEN_MARKER, *source.sessions]

    for name, data in contents.items():
        for secret in forbidden:
            assert secret.encode() not in data, name
    (tmp_path / "archived.db").write_bytes(contents["kyapehnu.db"])
    database = sqlite3.connect(tmp_path / "archived.db")
    dump = "\n".join(database.iterdump())
    database.close()
    assert all(secret not in dump for secret in forbidden)
    assert "secrets.json" not in contents and "config.yaml" not in contents


# ---- AC-31: backup, wipe, restore -------------------------------------------------------------------------------


def stack_view(data_dir: Path) -> dict:
    """What a person sees of the stack: accounts, profiles, items and image bytes, by account."""
    con = sqlite3.connect(data_dir / "kyapehnu.db")
    try:
        view = {
            "users": con.execute(
                "SELECT id, google_sub, email, display_name, is_admin FROM user ORDER BY id"
            ).fetchall(),
            "profiles": con.execute("SELECT user_id, home_city FROM userprofile ORDER BY user_id").fetchall(),
            "items": con.execute(
                "SELECT id, user_id, name, notes, cutout_path, thumb_path FROM item ORDER BY id"
            ).fetchall(),
            "revision": con.execute("SELECT version_num FROM alembic_version").fetchall(),
        }
    finally:
        con.close()
    view["images"] = {
        p.relative_to(data_dir).as_posix(): p.read_bytes()
        for p in sorted((data_dir / "images").rglob("*"))
        if p.is_file()
    }
    return view


@pytest.mark.parametrize("target", ["missing", "migrated-empty"])
def test_both_users_get_their_accounts_items_and_images_back_after_a_wipe(source, archive, cli_restore, target):
    before = stack_view(source.data_dir)
    shutil.rmtree(source.data_dir)  # the wipe
    if target == "migrated-empty":  # the stack was started once, so it has an empty database
        prepare_database(source.data_dir).dispose()

    cli_restore(source.data_dir, archive)

    after = stack_view(source.data_dir)
    assert after == before
    assert after["revision"] == [(HEAD_REVISION,)]
    assert [sum(1 for row in after["items"] if row[1] == uid) for uid in (source.a, source.b)] == [3, 2]
    engine = prepare_database(source.data_dir)  # the startup rule accepts the restored stack
    with Session(engine) as s:
        assert user_id_for_email(s, EMAILS["a"]) == source.a  # the same Google account signs in as the same person
        assert user_id_for_email(s, EMAILS["b"]) == source.b
    engine.dispose()


def test_a_restore_leaves_no_staging_files_and_no_secrets_behind(source, archive, cli_restore):
    shutil.rmtree(source.data_dir)

    cli_restore(source.data_dir, archive)

    assert {p.name for p in source.data_dir.iterdir()} <= {
        "kyapehnu.db",
        "kyapehnu.db-wal",
        "kyapehnu.db-shm",
        "images",
    }


def test_the_images_are_in_place_before_the_database_and_it_is_the_last_replace(
    source, archive, cli_restore, monkeypatch
):
    shutil.rmtree(source.data_dir)
    calls = []
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        images = source.data_dir / "images"
        present = {p.relative_to(source.data_dir).as_posix() for p in images.rglob("*") if p.is_file()}
        calls.append((Path(src), Path(dst), present))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)

    cli_restore(source.data_dir, archive)

    database_calls = [c for c in calls if c[1].resolve() == (source.data_dir / "kyapehnu.db").resolve()]
    assert len(database_calls) == 1 and calls[-1] is database_calls[0]
    src, _dst, present = database_calls[0]
    assert present == set(source.files)  # every image was already there
    assert src.resolve().is_relative_to(source.data_dir.resolve())  # same volume: a replace across devices fails


def test_a_restore_that_fails_while_copying_images_leaves_no_database_and_can_be_repeated(source, archive, cli_restore):
    shutil.rmtree(source.data_dir)
    source.data_dir.mkdir()
    (source.data_dir / "images").write_text("a file where the image folder must go")

    with pytest.raises((Exception, SystemExit)):
        cli_restore(source.data_dir, archive)

    assert not (source.data_dir / "kyapehnu.db").exists()  # no half-restored stack that a second run would refuse
    assert [p.name for p in source.data_dir.iterdir()] == ["images"]  # and no staging files
    (source.data_dir / "images").unlink()
    cli_restore(source.data_dir, archive)
    assert [row[0] for row in sql(source.data_dir, "SELECT id FROM user ORDER BY id")] == [source.a, source.b]


def test_a_restore_that_fails_at_the_last_step_removes_the_images_and_their_folders(
    source, archive, cli_restore, monkeypatch
):
    shutil.rmtree(source.data_dir)

    def fail(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)  # the database goes in last: the images are already there
    with pytest.raises((Exception, SystemExit)):
        cli_restore(source.data_dir, archive)

    assert not (source.data_dir / "kyapehnu.db").exists()
    assert not (source.data_dir / "images").exists() or list((source.data_dir / "images").iterdir()) == []


# ---- refusals: nothing is written ------------------------------------------------------------------------------


def refuse(data_dir: Path, archive: Path, cli_restore, capsys) -> str:
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cli_restore(data_dir, archive)
    out = capsys.readouterr()
    assert exc.value.code == 2
    assert out.err.strip()
    assert not any(email in out.out + out.err for email in EMAILS.values())
    return out.err


def target_state(data_dir: Path) -> dict:
    """The stack a refused restore must leave as it was: which files exist besides the database, which accounts."""
    if not data_dir.exists():
        return {"exists": False}
    files = sorted(
        p.relative_to(data_dir).as_posix()
        for p in data_dir.rglob("*")
        if p.is_file() and not p.name.startswith("kyapehnu.db")
    )
    has_db = (data_dir / "kyapehnu.db").exists()
    users = sql(data_dir, "SELECT id, google_sub FROM user ORDER BY id") if has_db else []
    return {"exists": True, "files": files, "users": users}


def assert_untouched_empty_target(target: Path) -> None:
    assert target_state(target) in ({"exists": False}, {"exists": True, "files": [], "users": []})


def assert_restores_into_an_empty_stack(archive: Path, tmp_path: Path, cli_restore) -> None:
    """The control of every refusal: it is the target or the crafted archive that is wrong, not the restore."""
    cli_restore(tmp_path / "control-stack", archive)
    assert sql(tmp_path / "control-stack", "SELECT count(*) FROM user") == [(2,)]


def test_a_stack_that_already_has_an_account_is_refused_and_not_touched(archive, tmp_path, cli_restore, capsys):
    other = tmp_path / "in-use"
    prepare_database(other).dispose()
    sql(
        other,
        "INSERT INTO user (google_sub, email, display_name, is_admin, created_at, last_sign_in_at)"
        " VALUES ('sub-z', 'z@example.com', 'Z', 1, ?, ?)",
        (NOW, NOW),
    )
    before = target_state(other)

    refuse(other, archive, cli_restore, capsys)

    assert target_state(other) == before
    assert before["users"] == [(1, "sub-z")]
    assert_restores_into_an_empty_stack(archive, tmp_path, cli_restore)


def test_restoring_into_the_stack_the_backup_came_from_is_refused(source, archive, cli_restore, capsys, tmp_path):
    before, view = target_state(source.data_dir), stack_view(source.data_dir)

    refuse(source.data_dir, archive, cli_restore, capsys)

    assert target_state(source.data_dir) == before
    assert stack_view(source.data_dir) == view
    assert_restores_into_an_empty_stack(archive, tmp_path, cli_restore)


BAD_MEMBERS = [
    "../evil.png",
    "images/../../evil.png",
    "images/1/cutout/../../../evil.png",
    "/abs/evil.png",
    "secrets.json",
    "config.yaml",
    "kyapehnu.db-wal",
    "backups/kyapehnu-old.zip",
    "images/cutout/a.png",
    "images/1/other/a.png",
    "images/1/cutout/a.txt",
    "images/x/cutout/a.png",
    "images/-1/cutout/a.png",
    "images/1/cutout/sub/a.png",
    "images/1/cutout/a b.png",
    "images/1/cutout/..png",
    "images/1/cutout/" + "a" * 201 + ".png",
    pytest.param(
        "images\\1\\cutout\\a.png",
        marks=pytest.mark.skipif(os.name == "nt", reason="zipfile turns a backslash into a slash on Windows"),
    ),
]


@pytest.mark.parametrize("name", BAD_MEMBERS)
def test_an_archive_with_a_member_outside_the_whitelist_is_refused_and_nothing_is_written(
    archive, tmp_path, cli_restore, capsys, name
):
    crafted = write_archive(tmp_path / "crafted.zip", {**members(archive), name: b"not an image of this stack"})
    target = tmp_path / "target"

    refuse(target, crafted, cli_restore, capsys)

    assert_untouched_empty_target(target)
    assert not (tmp_path / "evil.png").exists() and not (tmp_path.parent / "evil.png").exists()
    assert_restores_into_an_empty_stack(archive, tmp_path, cli_restore)


def test_an_archive_without_a_database_is_refused(archive, tmp_path, cli_restore, capsys):
    crafted = write_archive(
        tmp_path / "images-only.zip", {n: d for n, d in members(archive).items() if n != "kyapehnu.db"}
    )
    target = tmp_path / "target"

    refuse(target, crafted, cli_restore, capsys)

    assert_untouched_empty_target(target)
    assert_restores_into_an_empty_stack(archive, tmp_path, cli_restore)


@pytest.mark.parametrize("database", ["phase3", "unknown-revision", "not-sqlite"])
def test_an_archive_with_a_database_that_is_not_of_this_version_is_refused_and_nothing_is_written(
    archive, tmp_path, cli_restore, capsys, database
):
    bad = {
        "phase3": lambda: phase3_database(tmp_path, "a7d2e4f39c60"),
        "unknown-revision": lambda: stamped_database(tmp_path, "deadbeef0000"),
        "not-sqlite": lambda: os.urandom(RANDOM_BYTES),
    }[database]()
    crafted = write_archive(tmp_path / "crafted.zip", {**members(archive), "kyapehnu.db": bad})
    target = tmp_path / "target"

    refuse(target, crafted, cli_restore, capsys)

    assert_untouched_empty_target(target)
    assert_restores_into_an_empty_stack(archive, tmp_path, cli_restore)


def test_a_phase3_database_in_the_data_directory_is_left_alone_by_a_restore(archive, tmp_path, cli_restore, capsys):
    target = tmp_path / "target"
    target.mkdir()
    (target / "kyapehnu.db").write_bytes(phase3_database(tmp_path, "a7d2e4f39c60"))
    digest = hashlib.sha256((target / "kyapehnu.db").read_bytes()).hexdigest()

    refuse(target, archive, cli_restore, capsys)

    assert hashlib.sha256((target / "kyapehnu.db").read_bytes()).hexdigest() == digest
    assert not any(target.rglob("*.png"))
    assert_restores_into_an_empty_stack(archive, tmp_path, cli_restore)


@pytest.mark.parametrize("what", ["not-a-zip", "missing-file"])
def test_an_archive_that_cannot_be_read_is_refused(archive, tmp_path, cli_restore, capsys, what):
    unreadable = tmp_path / "broken.zip"
    if what == "not-a-zip":
        unreadable.write_bytes(os.urandom(RANDOM_BYTES))
    target = tmp_path / "target"

    refuse(target, unreadable, cli_restore, capsys)

    assert_untouched_empty_target(target)
    assert_restores_into_an_empty_stack(archive, tmp_path, cli_restore)
