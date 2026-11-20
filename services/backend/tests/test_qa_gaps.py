"""QA gap tests: behaviours added by the review cleanup that no unit test covered.

Folder ingest limits (AC-35), the 1 MiB JSON cap, the nosniff header, `GET /api` as a JSON 404,
backup collisions / missing DB / symlinks (AC-33), `create_*_job` refresh outside the compensation, and an
OpenAI-compatible 400 that is not about the response format (AC-9).
"""

import io
import json
import zipfile
from datetime import datetime

import httpx
import openai
import pytest
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, func, select

from kyapehnu.agent.llm.base import LLMUnavailableError
from kyapehnu.agent.llm.openai_compat import OpenAICompatTransport
from kyapehnu.core import backup as backup_module
from kyapehnu.core.backup import backup
from kyapehnu.core.config import MAX_JSON_BODY_BYTES, MAX_UPLOAD_FILE_BYTES, MAX_UPLOAD_FILES
from kyapehnu.core.errors import PayloadTooLargeError
from kyapehnu.files.local import LocalStore
from kyapehnu.wardrobe.ingest import pipeline
from kyapehnu.wardrobe.models import IngestJob, Photo
from tests.conftest import noise_image_bytes
from tests.fakes import OWNER_ID, ensure_owner, stored_files


@pytest.fixture
def owner(engine):
    with Session(engine) as s:
        ensure_owner(s)
        s.commit()


class Ping(BaseModel):
    ok: bool


def _folder(tmp_path, n):
    folder = tmp_path / "photos"
    folder.mkdir()
    for i in range(n):
        (folder / f"p{i}.jpg").write_bytes(b"x")
    return folder


def _nothing_stored(engine, data_dir):
    with Session(engine) as s:
        assert s.exec(select(func.count()).select_from(IngestJob)).one() == 0
    assert stored_files(data_dir) == []


# --- folder ingest is bounded like an upload (AC-35) --------------------------------------------------------


def test_folder_with_201_images_is_413_and_nothing_is_copied(client, tmp_path):
    folder = _folder(tmp_path, MAX_UPLOAD_FILES + 1)
    ctx = client.app.state.ctx

    with Session(ctx.engine) as s, pytest.raises(PayloadTooLargeError):
        pipeline.create_folder_job(OWNER_ID, s, ctx.files, folder)

    _nothing_stored(ctx.engine, ctx.data_dir)


def test_folder_with_exactly_200_images_is_accepted(client, tmp_path):
    folder = _folder(tmp_path, MAX_UPLOAD_FILES)
    ctx = client.app.state.ctx

    with Session(ctx.engine) as s:
        job = pipeline.create_folder_job(OWNER_ID, s, ctx.files, folder)

    assert job.total == MAX_UPLOAD_FILES


def test_folder_with_one_image_over_25mb_is_413_and_nothing_is_copied(client, tmp_path):
    folder = _folder(tmp_path, 1)
    with (folder / "big.jpg").open("wb") as f:
        f.truncate(MAX_UPLOAD_FILE_BYTES + 1)  # sparse: no real disk use
    ctx = client.app.state.ctx

    with Session(ctx.engine) as s, pytest.raises(PayloadTooLargeError):
        pipeline.create_folder_job(OWNER_ID, s, ctx.files, folder)

    _nothing_stored(ctx.engine, ctx.data_dir)


# --- 1 MiB cap on non-upload bodies -------------------------------------------------------------------------


def test_json_body_over_1_mib_is_413_by_declared_length(client):
    r = client.put(
        "/api/profile", content=b" " * (MAX_JSON_BODY_BYTES + 1), headers={"content-type": "application/json"}
    )

    assert (r.status_code, r.json()["code"]) == (413, "payload_too_large")


def test_json_body_over_1_mib_without_content_length_is_413(client):
    def chunks():
        yield b" " * MAX_JSON_BODY_BYTES
        yield b" "  # one byte over, no Content-Length header (chunked)

    r = client.put("/api/profile", content=chunks(), headers={"content-type": "application/json"})

    assert (r.status_code, r.json()["code"]) == (413, "http_413")  # the handlers' envelope for a raised HTTPException


def test_json_body_of_exactly_1_mib_passes_the_guard(client):
    body = b"[" + b" " * (MAX_JSON_BODY_BYTES - 2) + b"]"

    r = client.put("/api/profile", content=body, headers={"content-type": "application/json"})

    assert r.status_code == 422  # valid JSON of the wrong shape: it got past the size guard


# --- X-Content-Type-Options on every response ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "headers", "status"),
    [
        ("/api/items", {}, 200),
        ("/api/items/999999", {}, 404),
        ("/images/1/original/missing.jpg", {}, 404),
        ("/api/items", {"host": "evil.example"}, 400),  # rejected by the guard itself
    ],
)
def test_every_response_carries_nosniff(client, path, headers, status):
    r = client.get(path, headers=headers)

    assert r.status_code == status
    assert r.headers["x-content-type-options"] == "nosniff"


def test_an_uploaded_original_is_served_with_nosniff(client):
    client.post("/api/ingest", files=[("files", ("a.jpg", noise_image_bytes(1), "image/jpeg"))])
    client.app.state.worker.drain()
    [name] = [n for n in stored_files(client.app.state.ctx.data_dir) if "_norm" not in n]

    r = client.get(f"/images/1/original/{name}")

    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"


# --- GET /api (no slash) is a JSON 404 ---------------------------------------------------------------------


def test_get_api_without_slash_is_a_json_404(client):
    r = client.get("/api")

    assert r.status_code == 404
    assert r.json()["code"]


# --- backup (AC-33) -----------------------------------------------------------------------------------------


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 5, 12, 0, 0)


def test_two_backups_in_the_same_second_keep_both_archives(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(backup_module, "datetime", _Frozen)

    first, second = backup(tmp_path, LocalStore(tmp_path)), backup(tmp_path, LocalStore(tmp_path))

    assert first != second
    assert {p.name for p in (tmp_path / "backups").iterdir()} == {first.name, second.name}
    for path in (first, second):
        with zipfile.ZipFile(path) as z:
            assert z.testzip() is None and "kyapehnu.db" in z.namelist()


def test_a_failed_backup_never_deletes_an_earlier_one_with_the_same_second(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(backup_module, "datetime", _Frozen)
    good = backup(tmp_path, LocalStore(tmp_path))
    good_bytes = good.read_bytes()
    (tmp_path / "images" / "1" / "thumb").mkdir(parents=True)
    (tmp_path / "images" / "1" / "thumb" / "a.webp").write_bytes(b"t")

    def boom(self, key):
        raise OSError("disk full")

    monkeypatch.setattr(LocalStore, "open", boom)  # the picture cannot be read: the second archive fails half way
    with pytest.raises(OSError):
        backup(tmp_path, LocalStore(tmp_path))

    assert [p.name for p in (tmp_path / "backups").iterdir()] == [good.name]
    assert good.read_bytes() == good_bytes


def test_backup_of_a_missing_database_fails_and_creates_nothing(tmp_path):
    (tmp_path / "images").mkdir(exist_ok=True)

    with pytest.raises(Exception, match="unable to open database"):
        backup(tmp_path, LocalStore(tmp_path))

    assert not (tmp_path / "kyapehnu.db").exists()
    assert list((tmp_path / "backups").iterdir()) == []


def test_backup_skips_symlinked_images(engine, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("do not archive")
    (tmp_path / "images" / "1" / "thumb").mkdir(parents=True)
    (tmp_path / "images" / "1" / "thumb" / "real.webp").write_bytes(b"real")
    try:
        (tmp_path / "images" / "1" / "thumb" / "link.webp").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not supported here")

    with zipfile.ZipFile(backup(tmp_path, LocalStore(tmp_path))) as z:
        assert set(z.namelist()) == {"kyapehnu.db", "images/1/thumb/real.webp"}


# --- a failed refresh after the commit must not delete the originals ---------------------------------------


def _fail_refresh(monkeypatch):
    def refresh(self, *a, **k):
        raise OperationalError("refresh", {}, Exception("database is locked"))

    monkeypatch.setattr(Session, "refresh", refresh)


def _committed(engine, data_dir, n):
    with Session(engine) as s:
        assert s.exec(select(func.count()).select_from(IngestJob)).one() == 1
        assert s.exec(select(func.count()).select_from(Photo)).one() == n
    assert len(stored_files(data_dir)) == n


def test_upload_job_keeps_originals_when_the_refresh_after_commit_fails(make_ctx, owner, monkeypatch):
    ctx = make_ctx([])
    _fail_refresh(monkeypatch)

    with Session(ctx.engine) as s, pytest.raises(OperationalError):
        pipeline.create_upload_job(OWNER_ID, s, ctx.files, [("a.jpg", io.BytesIO(noise_image_bytes(1)))])

    _committed(ctx.engine, ctx.data_dir, 1)


def test_folder_job_keeps_originals_when_the_refresh_after_commit_fails(make_ctx, owner, tmp_path, monkeypatch):
    ctx = make_ctx([])
    src = _folder(tmp_path, 2)
    _fail_refresh(monkeypatch)

    with Session(ctx.engine) as s, pytest.raises(OperationalError):
        pipeline.create_folder_job(OWNER_ID, s, ctx.files, src)

    _committed(ctx.engine, ctx.data_dir, 2)


# --- OpenAI-compatible: a 400 that is not about the format (AC-9) -------------------------------------------


def _chat_ok():
    return httpx.Response(
        200,
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": '{"ok": true}'}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _transport(handler):
    client = openai.OpenAI(
        base_url="http://test/v1",
        api_key="k",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return OpenAICompatTransport("http://test/v1", "k", "m", client=client)


def _call(t):
    return t.call_json(system="s", prompt="p", images=[], schema=Ping.model_json_schema(), name="Ping")


def test_a_400_that_is_not_about_the_format_does_not_disable_json_schema():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["response_format"]["type"])
        if len(seen) == 1:
            return httpx.Response(400, json={"error": {"message": "image too large"}})
        return _chat_ok()

    t = _transport(handler)
    assert _call(t).text == '{"ok": true}'  # this request still succeeds through the fallback
    assert _call(t).text == '{"ok": true}'

    assert seen == ["json_schema", "json_object", "json_schema"]  # the next call tries the schema again


def test_a_400_on_both_formats_is_unavailable_and_keeps_json_schema():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["response_format"]["type"])
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    t = _transport(handler)
    with pytest.raises(LLMUnavailableError):
        _call(t)
    with pytest.raises(LLMUnavailableError):
        _call(t)

    assert seen == ["json_schema", "json_object"] * 2
