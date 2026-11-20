"""HTTP API through the real app: routes, error envelope, upload limits, loopback authorization, app wiring."""

import errno
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

import kyapehnu
from kyapehnu.agent.llm.base import LLMUnavailableError
from kyapehnu.agent.models import LlmCall
from kyapehnu.core.config import MAX_UPLOAD_FILE_BYTES, MAX_UPLOAD_FILES, Settings
from kyapehnu.core.errors import InsufficientStorageError
from kyapehnu.wardrobe.ingest import images, pipeline
from kyapehnu.wardrobe.ingest.jobs import Worker
from kyapehnu.wardrobe.models import IngestJob, IngestPiece, Item, Photo
from tests.conftest import LOOPBACK_PEER, noise_image_bytes
from tests.fakes import OWNER_ID, add_item, analysis, garment, stored_files

GENERATED_ID = re.compile(r"[0-9a-f]{32}")
BARE_BUILTIN_RAISE = re.compile(r"raise (ValueError|TypeError|KeyError|RuntimeError)\b")


# --- helpers ------------------------------------------------------------------------------------------------


def _db(client) -> Session:
    return Session(client.app.state.ctx.engine)


def _rows(client, model) -> list:
    with _db(client) as s:
        return list(s.exec(select(model).order_by(model.id)).all())


def _data_dir(client) -> Path:
    return client.app.state.settings.data_dir


def _upload(client, *blobs, names=None):
    names = names or [f"p{i}.jpg" for i in range(len(blobs))]
    files = [("files", (name, blob, "image/jpeg")) for name, blob in zip(names, blobs, strict=True)]
    return client.post("/api/ingest", files=files)


def _ingest(client, fake, *replies, n=1, seed=100):
    """Upload n distinct photos with scripted LLM replies and wait for the worker."""
    fake.replies.extend(replies)
    r = _upload(client, *[noise_image_bytes(seed + i) for i in range(n)])
    assert r.status_code == 201, r.text
    client.app.state.worker.drain()
    return r.json()


def _pieces(client, **params):
    return client.get("/api/ingest/pieces", params=params).json()


def _accepted_item(client, fake, seed=100, **tags):
    _ingest(client, fake, analysis(garment(**tags)), seed=seed)
    [piece] = [p for p in _pieces(client) if p["state"] == "pending_review"]
    r = client.post(f"/api/ingest/pieces/{piece['id']}/accept", json={})
    assert r.status_code == 200, r.text
    return r.json()


def _add_item(client, **fields) -> int:
    base = {
        "name": "Thing",
        "category": "top",
        "colors": [{"hex": "#001f3f", "name": "navy", "role": "primary"}],
        "formality": 3,
        "warmth": 2,
        "seasons": ["summer"],
        "tags": [],
    }
    with _db(client) as s:
        item = Item(**{"user_id": OWNER_ID, **base, **fields})
        s.add(item)
        s.commit()
        return item.id


def _job_count(client) -> int:
    with _db(client) as s:
        return s.exec(select(func.count()).select_from(IngestJob)).one()


def _assert_nothing_stored(client):
    assert _job_count(client) == 0
    assert stored_files(_data_dir(client)) == []


def _folder_with_images(tmp_path, *names):
    folder = tmp_path / "photos"
    folder.mkdir()
    for i, name in enumerate(names):
        (folder / name).write_bytes(noise_image_bytes(300 + i, fmt="PNG" if name.endswith(".png") else "JPEG"))
    return folder


# --- AC-12: upload ------------------------------------------------------------------------------------------


def test_upload_two_photos_creates_job_that_reaches_done(client, fake):
    fake.replies.extend([analysis(garment()), analysis(garment())])

    r = _upload(client, noise_image_bytes(1), noise_image_bytes(2))

    assert r.status_code == 201
    job = r.json()
    assert job["total"] == 2
    assert r.headers["location"] == f"/api/ingest/jobs/{job['id']}"
    client.app.state.worker.drain()
    done = client.get(f"/api/ingest/jobs/{job['id']}").json()
    assert (done["status"], done["processed"], done["failed"]) == ("done", 2, 0)
    assert len(stored_files(_data_dir(client))) == 4  # two originals and their normalized copies


def test_full_flow_upload_review_accept_edit(client, fake):
    job = _ingest(client, fake, analysis(garment(name="Blue kurta", category="ethnic")))
    assert client.get(f"/api/ingest/jobs/{job['id']}").json()["status"] == "done"
    [piece] = _pieces(client)
    assert piece["state"] == "pending_review"
    assert client.get(f"/{piece['thumb_path']}").status_code == 200

    item = client.post(f"/api/ingest/pieces/{piece['id']}/accept", json={"overrides": {"name": "Indigo kurta"}}).json()
    assert item["name"] == "Indigo kurta" and item["ai_tags"]["name"] == "Blue kurta"

    r = client.patch(f"/api/items/{item['id']}", json={"formality": 4, "name": None, "brand": "Fabindia"})
    assert r.status_code == 200
    assert (r.json()["formality"], r.json()["name"], r.json()["brand"]) == (4, "Indigo kurta", "Fabindia")
    detail = client.get(f"/api/items/{item['id']}").json()
    assert {c["field"] for c in detail["corrections"]} == {"name", "formality"}
    assert detail["photo_path"].startswith("images/1/original/")

    assert [i["name"] for i in client.get("/api/items", params={"category": "ethnic"}).json()] == ["Indigo kurta"]
    assert client.post(f"/api/items/{item['id']}/status", json={"status": "laundry"}).json()["status"] == "laundry"


# --- AC-13, AC-43: unsupported uploads ----------------------------------------------------------------------


def test_upload_unsupported_file_400_with_code_and_nothing_stored(client):
    r = _upload(client, noise_image_bytes(1), b"hi", names=["ok.jpg", "notes.txt"])

    assert r.status_code == 400
    body = r.json()
    assert (body["code"], "notes.txt" in body["detail"]) == ("unsupported_file", True)
    assert GENERATED_ID.fullmatch(body["request_id"])
    _assert_nothing_stored(client)


def test_upload_heic_without_extra_400_says_to_export_jpeg(client, monkeypatch):
    monkeypatch.setattr(images, "HEIC_AVAILABLE", False)

    r = _upload(client, b"not really heic", names=["IMG_1.HEIC"])

    assert (r.status_code, r.json()["code"]) == (400, "heic_extra_missing")
    assert r.json()["detail"] == "HEIC photos are not supported. Export them as JPEG."
    _assert_nothing_stored(client)


# --- AC-35: upload limits (413, nothing stored) -------------------------------------------------------------


def test_upload_201_files_413_nothing_stored(client):
    files = [("files", (f"p{i}.jpg", b"x", "image/jpeg")) for i in range(MAX_UPLOAD_FILES + 1)]

    r = client.post("/api/ingest", files=files)

    assert (r.status_code, r.json()["code"]) == (413, "payload_too_large")
    assert GENERATED_ID.fullmatch(r.json()["request_id"])
    _assert_nothing_stored(client)


def test_upload_exactly_200_files_accepted(client):
    files = [("files", (f"p{i}.jpg", b"x", "image/jpeg")) for i in range(MAX_UPLOAD_FILES)]

    r = client.post("/api/ingest", files=files)

    assert (r.status_code, r.json()["total"]) == (201, MAX_UPLOAD_FILES)
    client.app.state.worker.drain()  # each undecodable photo fails on its own, no LLM call is made


def test_upload_file_over_25mb_413_nothing_stored(client):
    big = b"\0" * (MAX_UPLOAD_FILE_BYTES + 1)

    r = _upload(client, noise_image_bytes(1), big, names=["ok.jpg", "big.jpg"])

    assert (r.status_code, r.json()["code"]) == (413, "payload_too_large")
    assert "big.jpg" in r.json()["detail"]
    _assert_nothing_stored(client)


def test_upload_file_of_exactly_25mb_accepted(client):
    r = _upload(client, b"\0" * MAX_UPLOAD_FILE_BYTES, names=["edge.jpg"])

    assert (r.status_code, r.json()["total"]) == (201, 1)
    client.app.state.worker.drain()


def test_disk_full_during_upload_gives_507_and_leaves_nothing(client, monkeypatch):
    def full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(pipeline, "_new_job", full)

    r = _upload(client, noise_image_bytes(1))

    assert (r.status_code, r.json()["code"]) == (507, "insufficient_storage")
    assert r.json()["detail"] == InsufficientStorageError.message
    assert stored_files(_data_dir(client)) == []


def test_disk_full_while_spooling_the_multipart_body_gives_507_and_leaves_nothing(client, monkeypatch):
    """Starlette spools each part to a temp file before the route runs; FastAPI wraps a failure there into a 400."""
    from starlette import formparsers

    class FullSpool(formparsers.SpooledTemporaryFile):
        def write(self, *_args, **_kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", FullSpool)

    r = _upload(client, noise_image_bytes(1))

    assert (r.status_code, r.json()["code"]) == (507, "insufficient_storage")
    assert r.json()["detail"] == InsufficientStorageError.message
    _assert_nothing_stored(client)


# --- review actions (AC-24..26 through HTTP) ----------------------------------------------------------------


def test_accepting_twice_is_conflict(client, fake):
    _ingest(client, fake, analysis(garment()))
    [piece] = _pieces(client)
    assert client.post(f"/api/ingest/pieces/{piece['id']}/accept", json={}).status_code == 200

    r = client.post(f"/api/ingest/pieces/{piece['id']}/accept", json={})

    assert (r.status_code, r.json()["code"]) == (409, "review_conflict")
    assert GENERATED_ID.fullmatch(r.json()["request_id"])


@pytest.mark.parametrize("action", ["discard", "mark-duplicate", "merge"])
def test_review_actions_on_an_accepted_piece_409(client, fake, action):
    item = _accepted_item(client, fake)
    piece_id = _rows(client, IngestPiece)[0].id
    bodies = {"discard": None, "mark-duplicate": {"item_id": item["id"]}, "merge": {"other_piece_ids": []}}

    r = client.post(f"/api/ingest/pieces/{piece_id}/{action}", json=bodies[action])

    assert (r.status_code, r.json()["code"]) == (409, "review_conflict")


def test_discard_piece_removes_it_from_the_default_queue(client, fake):
    _ingest(client, fake, analysis(garment()))
    [piece] = _pieces(client)

    r = client.post(f"/api/ingest/pieces/{piece['id']}/discard")

    assert (r.status_code, r.json()["state"]) == (200, "discarded")
    assert _pieces(client) == []
    assert [p["id"] for p in _pieces(client, state="discarded")] == [piece["id"]]
    assert _pieces(client, state="discarded", job_id=piece["job_id"] + 1) == []
    assert client.get("/api/items").json() == []


def test_mark_duplicate_links_the_item_and_creates_nothing(client, fake):
    item = _accepted_item(client, fake, seed=100)
    _ingest(client, fake, analysis(garment(name="Jeans", category="bottom")), seed=200)  # no dedupe candidate
    [piece] = _pieces(client)

    r = client.post(f"/api/ingest/pieces/{piece['id']}/mark-duplicate", json={"item_id": item["id"]})

    assert r.status_code == 200
    assert (r.json()["state"], r.json()["duplicate_of_item_id"]) == ("discarded", item["id"])
    assert len(client.get("/api/items").json()) == 1


def test_mark_duplicate_of_a_missing_item_404(client, fake):
    _ingest(client, fake, analysis(garment()))
    [piece] = _pieces(client)

    r = client.post(f"/api/ingest/pieces/{piece['id']}/mark-duplicate", json={"item_id": 99999})

    assert (r.status_code, r.json()["code"]) == (404, "not_found")


def test_merge_two_pieces_of_one_photo(client, fake):
    _ingest(
        client,
        fake,
        analysis(garment(name="Kurta", category="ethnic"), garment(name="Dupatta", bbox=(0, 0.1, 0.5, 0.5))),
    )
    first, second = _pieces(client)

    r = client.post(f"/api/ingest/pieces/{first['id']}/merge", json={"other_piece_ids": [second["id"]]})

    assert (r.status_code, r.json()["id"], r.json()["state"]) == (200, first["id"], "pending_review")
    assert [p["id"] for p in _pieces(client)] == [first["id"]]
    assert [p["state"] for p in _pieces(client, state="merged")] == ["merged"]


def test_accept_with_invalid_overrides_422_and_piece_stays_pending(client, fake):
    _ingest(client, fake, analysis(garment()))
    [piece] = _pieces(client)

    r = client.post(f"/api/ingest/pieces/{piece['id']}/accept", json={"overrides": {"formality": 9}})

    assert r.status_code == 422
    assert [p["state"] for p in _pieces(client)] == ["pending_review"]


def test_pieces_state_filter_rejects_unknown_state(client):
    assert client.get("/api/ingest/pieces", params={"state": "bogus"}).status_code == 422


def test_jobs_list_is_newest_first_and_capped_at_20(client):
    with _db(client) as s:
        s.add_all([IngestJob(user_id=OWNER_ID, source="upload") for _ in range(21)])
        s.commit()

    jobs = client.get("/api/ingest/jobs").json()

    assert len(jobs) == 20
    assert [j["id"] for j in jobs] == sorted((j["id"] for j in jobs), reverse=True)
    assert jobs[0]["id"] == 21


# --- retry (AC-18, C4) --------------------------------------------------------------------------------------


def test_error_piece_retry(client, fake):
    _ingest(client, fake, LLMUnavailableError("offline"))
    [piece] = _pieces(client)
    assert piece["state"] == "error"
    assert [p.status for p in _rows(client, Photo)] == ["failed"]
    fake.replies.append(analysis(garment()))

    r = client.post(f"/api/ingest/photos/{piece['photo_id']}/retry")

    assert (r.status_code, r.json()) == (202, {"queued": True})
    client.app.state.worker.drain()
    assert [p["state"] for p in _pieces(client)] == ["pending_review"]


@pytest.mark.parametrize("status", ["pending", "done"])
def test_retry_of_a_photo_that_has_not_failed_409(client, fake, status):
    with _db(client) as s:
        job = IngestJob(user_id=OWNER_ID, source="upload", total=1)
        s.add(job)
        s.flush()
        photo = Photo(user_id=OWNER_ID, job_id=job.id, original_path="images/1/original/x.jpg", status=status)
        s.add(photo)
        s.commit()
        photo_id = photo.id

    r = client.post(f"/api/ingest/photos/{photo_id}/retry")

    assert (r.status_code, r.json()["code"]) == (409, "conflict")
    client.app.state.worker.drain()
    assert fake.calls == []
    assert [p.status for p in _rows(client, Photo)] == [status]


# --- 404 mapping (D4, D5) -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/items/999999", None),
        ("PATCH", "/api/items/999999", {}),
        ("POST", "/api/items/999999/status", {"status": "laundry"}),
        ("DELETE", "/api/items/999999", None),
        ("DELETE", "/api/items/999999?hard=true", None),
        ("GET", "/api/ingest/jobs/999999", None),
        ("POST", "/api/ingest/pieces/999999/accept", {}),
        ("POST", "/api/ingest/pieces/999999/discard", None),
        ("POST", "/api/ingest/pieces/999999/merge", {"other_piece_ids": [1]}),
        ("POST", "/api/ingest/pieces/999999/mark-duplicate", {"item_id": 1}),
        ("POST", "/api/ingest/photos/999999/retry", None),
    ],
)
def test_missing_resource_404_not_found_code(client, method, path, body):
    r = client.request(method, path, json=body)

    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert GENERATED_ID.fullmatch(r.json()["request_id"])


def test_unknown_status_value_422(client, fake):
    item = _accepted_item(client, fake)

    assert client.post(f"/api/items/{item['id']}/status", json={"status": "bogus"}).status_code == 422


# --- AC-6, C11: item PATCH ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        {"formality": 0},
        {"formality": 6},
        {"formality": 9},
        {"warmth": 0},
        {"warmth": 6},
        {"category": "hat"},
        {"colors": []},
        {"purchase_price": -1},
    ],
)
def test_invalid_patch_422(client, fake, patch):
    item = _accepted_item(client, fake)

    r = client.patch(f"/api/items/{item['id']}", json=patch)

    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)  # FastAPI's own 422 body is kept (the frontend schema uses it)
    assert client.get(f"/api/items/{item['id']}").json()["item"]["formality"] == item["formality"]


@pytest.mark.parametrize("value", [1, 5])
def test_patch_formality_and_warmth_boundaries_accepted(client, fake, value):
    item = _accepted_item(client, fake)

    r = client.patch(f"/api/items/{item['id']}", json={"formality": value, "warmth": value})

    assert (r.status_code, r.json()["formality"], r.json()["warmth"]) == (200, value, value)


@pytest.mark.parametrize(
    "field", ["name", "category", "subcategory", "colors", "pattern", "formality", "warmth", "seasons", "tags", "notes"]
)
def test_patch_null_for_a_required_field_is_ignored(client, fake, field):
    item = _accepted_item(client, fake)

    r = client.patch(f"/api/items/{item['id']}", json={field: None})

    assert r.status_code == 200
    assert r.json()[field] == item[field]


def test_patch_null_clears_nullable_fields(client, fake):
    item = _accepted_item(client, fake)  # the garment helper tags fabric "cotton" and fit "regular"
    assert (item["fabric"], item["fit"]) == ("cotton", "regular")
    client.patch(
        f"/api/items/{item['id']}", json={"brand": "Fabindia", "purchase_price": 1999.5, "purchase_date": "2026-01-02"}
    )

    r = client.patch(
        f"/api/items/{item['id']}",
        json={"fabric": None, "fit": None, "brand": None, "purchase_price": None, "purchase_date": None},
    )

    assert r.status_code == 200
    assert [r.json()[f] for f in ("fabric", "fit", "brand", "purchase_price", "purchase_date")] == [None] * 5


# --- AC-27: list filters ------------------------------------------------------------------------------------


@pytest.fixture
def wardrobe(client):
    _add_item(client, name="Navy shirt", brand="Fabindia", tags=["office"])
    _add_item(
        client,
        name="Red kurta",
        category="ethnic",
        colors=[{"hex": "#ff0000", "name": "red", "role": "primary"}],
        formality=4,
        seasons=["winter", "autumn"],
        status="laundry",
    )
    _add_item(
        client,
        name="Black jeans",
        category="bottom",
        colors=[{"hex": "#000000", "name": "black", "role": "primary"}],
        formality=2,
        seasons=["winter"],
    )
    _add_item(client, name="Old tee", formality=1, status="retired")


@pytest.mark.parametrize(
    ("params", "names"),
    [
        ({}, ["Black jeans", "Red kurta", "Navy shirt"]),  # newest first, retired hidden
        ({"category": "top"}, ["Navy shirt"]),
        ({"color": "navy"}, ["Navy shirt"]),
        ({"color": "NAVY"}, ["Navy shirt"]),
        ({"formality_min": 3}, ["Red kurta", "Navy shirt"]),
        ({"formality_max": 2}, ["Black jeans"]),
        ({"formality_min": 3, "formality_max": 3}, ["Navy shirt"]),
        ({"season": "winter"}, ["Black jeans", "Red kurta"]),
        ({"status": "retired"}, ["Old tee"]),
        ({"status": "laundry"}, ["Red kurta"]),
        ({"q": "fabindia"}, ["Navy shirt"]),
        ({"q": "OFFICE"}, ["Navy shirt"]),
        ({"q": "kurta"}, ["Red kurta"]),
        ({"category": "top", "season": "summer"}, ["Navy shirt"]),
        ({"category": "ethnic", "season": "summer"}, []),
    ],
)
def test_list_items_filters(client, wardrobe, params, names):
    r = client.get("/api/items", params=params)

    assert r.status_code == 200
    assert [i["name"] for i in r.json()] == names


@pytest.mark.parametrize(
    "params", [{"formality_min": 0}, {"formality_max": 6}, {"category": "hat"}, {"season": "monsoony"}]
)
def test_list_items_invalid_filter_422(client, params):
    assert client.get("/api/items", params=params).status_code == 422


# --- AC-29, AC-30: delete and detail ------------------------------------------------------------------------


def test_soft_delete_retires_and_hides_from_the_default_list(client, fake):
    item = _accepted_item(client, fake)

    assert client.delete(f"/api/items/{item['id']}").status_code == 204

    assert client.get(f"/api/items/{item['id']}").json()["item"]["status"] == "retired"
    assert client.get("/api/items").json() == []
    assert [i["id"] for i in client.get("/api/items", params={"status": "retired"}).json()] == [item["id"]]


def test_hard_delete_removes_row_corrections_and_files(client, fake):
    item = _accepted_item(client, fake)
    client.patch(f"/api/items/{item['id']}", json={"name": "Renamed"})  # one correction
    data_dir = _data_dir(client)
    assert (data_dir / item["cutout_path"]).exists()

    r = client.delete(f"/api/items/{item['id']}", params={"hard": True})

    assert r.status_code == 204 and r.content == b""
    assert client.get(f"/api/items/{item['id']}").status_code == 404
    assert client.get(f"/{item['cutout_path']}").status_code == 404
    assert not (data_dir / item["cutout_path"]).exists()
    assert not (data_dir / item["thumb_path"]).exists()


def test_item_detail_has_siblings_corrections_and_photo_path(client, fake):
    _ingest(
        client, fake, analysis(garment(name="Shirt"), garment(name="Jeans", category="bottom", bbox=(0, 0.5, 1, 1)))
    )
    first, second = _pieces(client)
    a = client.post(f"/api/ingest/pieces/{first['id']}/accept", json={"overrides": {"formality": 5}}).json()
    b = client.post(f"/api/ingest/pieces/{second['id']}/accept", json={}).json()

    detail = client.get(f"/api/items/{a['id']}").json()

    assert [i["id"] for i in detail["siblings"]] == [b["id"]]
    assert [c["field"] for c in detail["corrections"]] == ["formality"]
    assert detail["photo_path"].startswith("images/1/original/")


def test_item_without_a_photo_has_no_siblings_and_no_photo_path(client):
    item_id = _add_item(client)  # like a seed item

    detail = client.get(f"/api/items/{item_id}").json()

    assert (detail["siblings"], detail["corrections"], detail["photo_path"]) == ([], [], None)


# --- AC-34: image store -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/images/%2e%2e/kyapehnu.db",
        "/images/..%2fkyapehnu.db",
        "/images/%2e%2e%2fkyapehnu.db",
        "/images/original/..%2f..%2fkyapehnu.db",
        "/images/%2fetc%2fpasswd",
    ],
)
def test_image_traversal_is_404_and_leaks_nothing(client, path):
    assert (_data_dir(client) / "kyapehnu.db").exists()  # the target the request is aiming at

    r = client.get(path)

    assert r.status_code == 404
    assert r.json()["code"] == "http_404"
    assert b"SQLite format 3" not in r.content


def test_missing_image_404_has_the_error_envelope(client):
    r = client.get("/images/original/nope.jpg")

    assert (r.status_code, r.json()["code"]) == (404, "http_404")
    assert GENERATED_ID.fullmatch(r.json()["request_id"])


# --- AC-3: the API stands alone (no SPA, JSON errors) -------------------------------------------------------


def test_unknown_api_path_is_a_json_404(client):
    r = client.get("/api/nope")

    assert r.status_code == 404
    assert set(r.json()) == {"detail", "code", "request_id"}


def test_api_reports_the_package_version(client):
    import kyapehnu

    assert client.app.openapi()["info"]["version"] == kyapehnu.__version__


# --- AC-4, AC-31: settings and profile ----------------------------------------------------------------------


def test_app_starts_with_defaults_and_reports_the_missing_key(tmp_path, fake, caplog):
    from kyapehnu.core.app import create_app

    s = Settings(
        _env_file=None, data_dir=tmp_path / "data", config_file=tmp_path / "missing.yaml", anthropic_api_key=None
    )

    with caplog.at_level(logging.WARNING):
        app = create_app(settings=s, transport=fake)
    with TestClient(app, base_url="http://localhost") as c:
        body = c.get("/api/settings").json()
        profile = c.get("/api/profile").json()

    assert body["llm_configured"] is False
    assert (body["region"], body["home_city"], body["currency"]) == ("IN", "Gurugram", "INR")
    assert (profile["home_city"], profile["budget_min"], profile["budget_max"]) == ("Gurugram", 1000, 5000)
    assert any("missing.yaml" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize(
    ("provider", "key", "configured"),
    [("anthropic", None, False), ("anthropic", "k", True), ("openai_compatible", None, True)],
)
def test_settings_llm_configured(tmp_path, fake, provider, key, configured):
    from kyapehnu.core.app import create_app

    s = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        config_file=tmp_path / "missing.yaml",
        llm_provider=provider,
        anthropic_api_key=key,
    )

    with TestClient(create_app(settings=s, transport=fake), base_url="http://localhost") as c:
        body = c.get("/api/settings").json()

    assert (body["provider"], body["llm_configured"]) == (provider, configured)


def test_settings_never_expose_secrets(settings, fake):
    from kyapehnu.core.app import create_app

    s = settings.model_copy(update={"anthropic_api_key": "sk-ant-SECRET", "openai_api_key": "sk-oai-SECRET"})

    with TestClient(create_app(settings=s, transport=fake), base_url="http://localhost") as c:
        r = c.get("/api/settings")

    assert r.status_code == 200
    assert r.json()["provider"] == "anthropic" and r.json()["llm_configured"] is True
    assert "SECRET" not in r.text


def test_test_llm_ok_is_logged_for_the_debug_panel(client, fake):
    fake.replies.append({"ok": True})

    body = client.post("/api/settings/test-llm").json()

    assert body["ok"] is True and body["error"] is None and body["latency_ms"] >= 0
    assert client.get("/api/debug/llm-calls").json()[0]["purpose"] == "test"


def test_test_llm_failure_is_reported_not_a_500(client, fake):
    fake.replies.append(LLMUnavailableError("offline"))

    r = client.post("/api/settings/test-llm")

    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["error"]


def test_test_llm_unexpected_provider_exception_is_reported_not_a_500(client, fake):
    fake.replies.append(ZeroDivisionError("provider exploded"))

    r = client.post("/api/settings/test-llm")

    assert (r.status_code, r.json()["ok"]) == (200, False)


def test_test_llm_never_returns_the_raw_model_reply(client, fake):
    """WP-2 review: LLMOutputError text carries the reply. It may echo anything, so it must not reach the body."""
    fake.replies.extend(["SECRET-RAW-REPLY not json", "SECRET-RAW-REPLY still not json"])

    r = client.post("/api/settings/test-llm")

    assert (r.status_code, r.json()["ok"]) == (200, False)
    assert "SECRET-RAW-REPLY" not in r.text


def test_test_llm_never_echoes_a_configured_secret(client, settings, fake):
    fake.replies.append(LLMUnavailableError(f"401 invalid x-api-key: {settings.anthropic_api_key}"))

    r = client.post("/api/settings/test-llm")

    assert (r.status_code, r.json()["ok"]) == (200, False)
    assert settings.anthropic_api_key not in r.text


def test_profile_roundtrip(client):
    p = client.get("/api/profile").json()
    assert p["home_city"] == "Gurugram"
    p["colors_loved"] = ["navy", "olive"]

    r = client.put("/api/profile", json=p)

    assert r.json()["colors_loved"] == ["navy", "olive"]
    assert client.get("/api/profile").json()["colors_loved"] == ["navy", "olive"]


def test_profile_survives_a_restart(settings, fake):
    from kyapehnu.core.app import create_app

    with TestClient(create_app(settings=settings, transport=fake), base_url="http://localhost") as c:
        profile = c.get("/api/profile").json()
        c.put("/api/profile", json={**profile, "home_city": "Mumbai"})

    with TestClient(create_app(settings=settings, transport=fake), base_url="http://localhost") as c:
        assert c.get("/api/profile").json()["home_city"] == "Mumbai"


def test_profile_put_rejects_wrong_types(client):
    p = client.get("/api/profile").json()

    assert client.put("/api/profile", json={**p, "colors_loved": "navy"}).status_code == 422


# --- AC-11: debug endpoint ----------------------------------------------------------------------------------


def test_debug_llm_calls_filters(client, fake):
    with _db(client) as s:
        s.add(
            LlmCall(
                user_id=1,
                provider="fake",
                model="m",
                purpose="tagging",
                request_summary="s",
                latency_ms=1,
                response="{}",
            )
        )
        s.commit()
    fake.replies.extend([{"ok": True}, LLMUnavailableError("offline")])
    client.post("/api/settings/test-llm")  # a "test" call that works
    client.post("/api/settings/test-llm")  # a "test" call that fails

    everything = client.get("/api/debug/llm-calls").json()
    tests = client.get("/api/debug/llm-calls", params={"purpose": "test"}).json()
    failed = client.get("/api/debug/llm-calls", params={"errors_only": True}).json()
    failed_tests = client.get("/api/debug/llm-calls", params={"purpose": "test", "errors_only": True}).json()

    assert [c["purpose"] for c in everything] == ["test", "test", "tagging"]  # newest first
    assert [c["purpose"] for c in tests] == ["test", "test"]
    assert [c["error"] is not None for c in failed] == [True] and failed[0]["purpose"] == "test"
    assert [c["id"] for c in failed_tests] == [failed[0]["id"]]
    assert client.get("/api/debug/llm-calls", params={"purpose": "nothing"}).json() == []
    assert len(client.get("/api/debug/llm-calls", params={"limit": 1}).json()) == 1


def test_ingestion_records_its_llm_call_without_waiting_on_the_database_lock(client, fake):
    """FR-5: the worker's tagging call must reach the Debug view. The log sink writes on its own connection, so it
    must not run while the worker's session holds an open write transaction (it would wait out SQLite's 5 s busy
    timeout and then lose the row)."""
    started = time.monotonic()

    _ingest(client, fake, analysis(garment()))

    assert time.monotonic() - started < 3
    calls = client.get("/api/debug/llm-calls", params={"purpose": "tagging"}).json()
    assert [c["error"] for c in calls] == [None]


def test_dedupe_call_is_recorded_without_waiting_on_the_database_lock(client, fake):
    """Same as above for the dedupe call: find_duplicate autoflushes the pending piece, which would open a write
    transaction before the log sink writes on its own connection (5 s busy wait, then the row is lost)."""
    _accepted_item(client, fake, seed=100)
    started = time.monotonic()

    _ingest(client, fake, analysis(garment()), {"match_index": None, "confidence": 0.0}, seed=900)

    assert time.monotonic() - started < 3
    calls = client.get("/api/debug/llm-calls", params={"purpose": "dedupe"}).json()
    assert [c["error"] for c in calls] == [None]
    assert len(client.get("/api/debug/llm-calls", params={"purpose": "tagging"}).json()) == 2


@pytest.mark.parametrize("limit", [0, 1001])
def test_debug_llm_calls_limit_bounds_422(client, limit):
    assert client.get("/api/debug/llm-calls", params={"limit": limit}).status_code == 422


# --- AC-40 (D17): logs ---------------------------------------------------------------------------------------


def test_logs_have_no_profile_or_bytes(settings, fake, tmp_path, caplog):
    from kyapehnu.core.app import create_app

    config = tmp_path / "config.yaml"
    config.write_text("region: IN\n")  # an existing file, so startup logs no "not found" warning with a path
    s = settings.model_copy(update={"config_file": config})
    folder = _folder_with_images(tmp_path, "f.jpg")
    jpeg = noise_image_bytes(200)
    fake.replies.extend(
        [analysis(garment(name="SENTINEL-GARMENT")), analysis(garment(name="SENTINEL-GARMENT")), {"ok": True}]
    )

    with (
        caplog.at_level(logging.INFO),
        TestClient(create_app(settings=s, transport=fake), base_url="http://localhost", client=LOOPBACK_PEER) as c,
    ):
        profile = c.get("/api/profile").json()
        for field in ("body_notes", "skin_tone", "style_prefs", "fit_prefs"):
            profile[field] = f"SENTINEL-{field}"
        assert c.put("/api/profile", json=profile).status_code == 200
        assert _upload(c, jpeg).status_code == 201
        c.app.state.worker.drain()
        with _db(c) as s:
            folder_job_id = pipeline.create_folder_job(OWNER_ID, s, c.app.state.ctx.files, folder).id
        c.app.state.worker.submit_job(folder_job_id)
        c.app.state.worker.drain()
        assert c.post("/api/settings/test-llm").json()["ok"] is True
        c.get("/api/settings")

    text = caplog.text
    assert "event=job_started" in text and "event=job_done" in text  # not vacuous
    for needle in (
        "SENTINEL-body_notes",
        "SENTINEL-skin_tone",
        "SENTINEL-style_prefs",
        "SENTINEL-fit_prefs",
        "SENTINEL-GARMENT",
        "test-key",
        str(tmp_path),
        "\\xff\\xd8",
        "\xff\xd8",
    ):
        assert needle not in text


# --- app wiring (D4, D11) -----------------------------------------------------------------------------------


def _expired_orphan(settings) -> Path:
    orphan = settings.data_dir / "images" / "1" / "original" / "orphan.jpg"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"x")
    two_hours_ago = time.time() - 2 * 3600
    os.utime(orphan, (two_hours_ago, two_hours_ago))
    return orphan


def _owner_with_a_row(app) -> None:
    """The sweep leaves a user with no rows alone (ADR-5), so the owner of the orphan needs one."""
    with Session(app.state.ctx.engine) as s:
        add_item(s)


def test_startup_runs_housekeeping_before_the_worker_starts(settings, fake, monkeypatch):
    from kyapehnu.core.app import create_app

    orphan = _expired_orphan(settings)
    orphan_present_when_worker_starts: list[bool] = []
    monkeypatch.setattr(Worker, "start", lambda _self: orphan_present_when_worker_starts.append(orphan.exists()))
    app = create_app(settings=settings, transport=fake)
    _owner_with_a_row(app)

    with TestClient(app, base_url="http://localhost"):
        pass

    assert orphan_present_when_worker_starts == [False]
    assert not orphan.exists()


def test_startup_without_worker_does_not_run_housekeeping(settings, fake):
    from kyapehnu.core.app import create_app

    orphan = _expired_orphan(settings)

    with TestClient(create_app(settings=settings, transport=fake, start_worker=False), base_url="http://localhost"):
        pass

    assert orphan.exists()


def test_worker_runs_while_the_app_is_up_and_stops_on_shutdown(settings, fake):
    from kyapehnu.core.app import create_app

    def workers():
        return [t for t in threading.enumerate() if t.name == "kyapehnu-ingest" and t.is_alive()]

    with TestClient(create_app(settings=settings, transport=fake), base_url="http://localhost"):
        assert len(workers()) == 1

    assert workers() == []


def test_handlers_are_wired_with_the_guards_request_id(client, add_route):
    from kyapehnu.core.errors import NotFoundError

    def missing() -> None:
        raise NotFoundError("Thing 7 not found")

    add_route("/__missing", missing)

    r = client.get("/__missing", headers={"x-request-id": "rid-wired-1"})

    assert r.status_code == 404
    assert r.json() == {"detail": "Thing 7 not found", "code": "not_found", "request_id": "rid-wired-1"}


def test_unexpected_error_in_a_route_gets_the_500_envelope(app, add_route):
    def boom() -> None:
        raise KeyError("secret")

    add_route("/__boom", boom)

    with TestClient(app, base_url="http://localhost", raise_server_exceptions=False) as c:
        r = c.get("/__boom", headers={"x-request-id": "rid-500"})

    assert r.status_code == 500
    assert r.json() == {"detail": "Internal error", "code": "internal", "request_id": "rid-500"}
    assert "secret" not in r.text and "KeyError" not in r.text


def test_no_bare_builtin_raises():
    """LLD 4 rule: request-reachable code raises AppError subclasses, never a bare builtin for user-caused errors."""
    root = Path(kyapehnu.__file__).parent
    files = list(root.rglob("*.py"))
    hits = [
        f"{path.relative_to(root)}:{n}"
        for path in files
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if BARE_BUILTIN_RAISE.search(line)
    ]

    assert len(files) > 20  # the scan really saw the package
    assert hits == []


def test_core_app_starts_without_pillow_heif(tmp_path):
    """AC-43 (WP-2 hand-over): the whole app, not only the image helpers, imports and serves without pillow-heif."""
    code = f"""
import sys
sys.modules['pillow_heif'] = None
from fastapi.testclient import TestClient
from kyapehnu.core.app import create_app
from kyapehnu.core.config import Settings
import kyapehnu.core.cli
import kyapehnu.wardrobe.ingest.images as i

class Transport:
    provider, model = 'stub', 'stub-1'

settings = Settings(_env_file=None, data_dir={str(tmp_path / "data")!r}, config_file={str(tmp_path / "none.yaml")!r})
app = create_app(settings=settings, transport=Transport(), start_worker=False)
with TestClient(app, base_url='http://localhost') as c:
    assert c.get('/livez').status_code == 200
    assert c.get('/api/items').status_code == 401  # nobody signed in: the API answers, the sign-in rule applies
sys.exit(i.HEIC_AVAILABLE)
"""
    result = subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


# --- Phase 2 wiring (WP-1): empty routers mounted, HTTP client loggers quiet ----------------------------------------


@pytest.mark.parametrize("module", ["outfits", "events"])
def test_phase2_routers_are_mounted_by_create_app(module, settings, fake):
    import importlib

    from kyapehnu.core.app import create_app

    router = importlib.import_module(f"kyapehnu.wardrobe.api.{module}").router
    path = f"/api/{module}/wp1-probe"
    router.add_api_route(path, lambda: {"ok": True}, methods=["GET"])
    try:  # a router with no routes cannot be seen from outside, so give it one for the duration of the test
        probe = TestClient(
            create_app(settings=settings, transport=fake, start_worker=False), base_url="http://localhost"
        )
        response = probe.get(path, headers={"Host": "localhost"})
    finally:
        router.routes[:] = [r for r in router.routes if getattr(r, "path", None) != path]

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize("name", ["httpx", "httpcore"])
def test_create_app_sets_the_http_client_loggers_to_warning(name, settings, fake):
    from kyapehnu.core.app import create_app

    logger = logging.getLogger(name)
    before = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        create_app(settings=settings, transport=fake, start_worker=False)
        level = logger.level
    finally:
        logger.setLevel(before)

    assert level == logging.WARNING
