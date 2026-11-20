"""RequestGuardMiddleware: request id, Host allowlist (AC-45), same-origin rule (AC-46), body cap (AC-35)."""

import asyncio
import json
import logging
import re

import pytest
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from kyapehnu.core import middleware
from kyapehnu.core.config import Settings
from kyapehnu.core.errors import InvalidInputError, PayloadTooLargeError, install_handlers
from kyapehnu.core.middleware import TOO_LARGE_MESSAGE, RequestGuardMiddleware, host_name
from kyapehnu.wardrobe.models import IngestJob
from tests.conftest import noise_image_bytes
from tests.fakes import stored_files

GENERATED_ID = re.compile(r"[0-9a-f]{32}")
MAX_ID_CHARS = 64
CAP = 10
OK_BODY = b'{"a":"xx"}'  # exactly CAP bytes, valid JSON
BIG_BODY = b'{"a":"xxx"}'  # CAP + 1 bytes, valid JSON


def _job_count(client) -> int:
    with Session(client.app.state.ctx.engine) as s:
        return s.exec(select(func.count()).select_from(IngestJob)).one()


def _original_files(client) -> list[str]:
    return stored_files(client.app.state.settings.data_dir)


def _jpeg_part(seed=1, name="a.jpg"):
    return ("files", (name, noise_image_bytes(seed), "image/jpeg"))


# --- Host allowlist (AC-45) ---------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/items", "/images/original/x.jpg", "/api/ingest/jobs"])
def test_foreign_host_400_json_envelope(client, path):
    r = client.get(path, headers={"host": "evil.example"})

    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["code"] == "invalid_host"
    assert body["detail"] == "Invalid host header"
    assert GENERATED_ID.fullmatch(body["request_id"])


def test_foreign_host_with_a_port_is_still_rejected(client):
    r = client.get("/api/items", headers={"host": "evil.example:8000"})

    assert (r.status_code, r.json()["code"]) == (400, "invalid_host")


def test_foreign_host_wins_over_cross_origin_and_never_reaches_the_route(client):
    r = client.post(
        "/api/ingest", files=[_jpeg_part()], headers={"host": "evil.example", "origin": "http://evil.example"}
    )

    assert (r.status_code, r.json()["code"]) == (400, "invalid_host")
    assert _job_count(client) == 0
    assert _original_files(client) == []


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("localhost:5173", "localhost"),
        ("LOCALHOST", "localhost"),
        ("127.0.0.1:8000", "127.0.0.1"),
        ("evil.example", "evil.example"),
        ("[::1]:8000", "::1"),
        ("evil@localhost", None),
        ("localhost@evil.example", None),
        ("[::1", None),
        ("", None),
    ],
)
def test_host_name_parsing(header, expected):
    assert host_name(header) == expected


@pytest.mark.parametrize(
    ("header", "allowed"),
    [
        ("localhost:5173", True),
        ("LOCALHOST", True),
        ("127.0.0.1:8000", True),
        ("evil.example", False),
        ("evil@localhost", False),
        ("[::1", False),
        ("", False),
        ("[::1]:8000", False),  # the server never listens on IPv6 (LLD 3.6)
        ("localhost.evil.example", False),
    ],
)
def test_host_allowlist_decision(header, allowed):
    assert (host_name(header) in Settings(_env_file=None, public_url="http://localhost").allowed_hosts) is allowed


@pytest.mark.parametrize(
    ("public_url", "hosts"),
    [
        ("http://localhost:8080", {"localhost", "127.0.0.1"}),
        ("https://kyapehnu.example", {"kyapehnu.example", "localhost", "127.0.0.1"}),
        ("http://192.168.1.20:8080/", {"192.168.1.20", "localhost", "127.0.0.1"}),
    ],
)
def test_allowed_hosts_come_from_the_public_url(public_url, hosts):
    assert Settings(_env_file=None, public_url=public_url).allowed_hosts == hosts


@pytest.mark.parametrize(
    ("host", "status"),
    [
        ("kyapehnu.example:8443", 200),
        ("localhost:8000", 200),
        ("192.168.1.20:8000", 400),  # no longer allowed just because it is this computer's address
        ("mycomputer.local:8000", 400),
    ],
)
def test_public_host_rule_end_to_end(settings, fake, kv, host, status):
    from kyapehnu.core.app import create_app

    app = create_app(
        settings=settings.model_copy(update={"public_url": "https://kyapehnu.example:8443"}), transport=fake, kv=kv
    )

    with TestClient(app, base_url="http://localhost") as c:
        assert c.get("/api/items", headers={"host": host}).status_code == status


def _scope(headers: dict[str, str], method: str = "POST", scope_type: str = "http") -> dict:
    return {
        "type": scope_type,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/x",
        "raw_path": b"/x",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("localhost", 8000),
    }


def _drive(guard, scope, chunks=(b"",)):
    """Run the guard once with a scripted body. Returns the messages it sent."""
    queue = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    sent: list[dict] = []

    async def receive():
        return queue.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(guard(scope, receive, send))
    return sent


def _reply(sent):
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body)


def _guard(inner, cap=CAP):
    return RequestGuardMiddleware(
        inner, max_body_bytes=cap, allowed_hosts=frozenset({"localhost"}), public_origin="http://localhost"
    )


def test_missing_host_header_400_and_inner_app_not_called():
    called = []

    async def inner(scope, receive, send):
        called.append(scope)

    status, body = _reply(_drive(_guard(inner), _scope({}, method="GET")))

    assert (status, body["code"]) == (400, "invalid_host")
    assert called == []


def test_non_http_scopes_pass_through_untouched():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    sent = _drive(_guard(inner), {"type": "lifespan"})

    assert seen == ["lifespan"]
    assert sent == []


def test_error_inside_the_guard_gets_the_json_500_envelope(settings, fake, monkeypatch):
    from kyapehnu.core.app import create_app

    def boom(_host: str) -> str:
        raise RuntimeError("secret-boom")

    monkeypatch.setattr(middleware, "host_name", boom)
    app = create_app(settings=settings, transport=fake)

    with TestClient(app, base_url="http://localhost", raise_server_exceptions=False) as c:
        r = c.get("/api/items", headers={"x-request-id": "rid-guard-1"})

    assert r.status_code == 500
    assert r.json() == {"detail": "Internal error", "code": "internal", "request_id": "rid-guard-1"}
    assert "secret-boom" not in r.text


# --- request id ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("rid", ["abc-1", "A.b_c-9", "x" * MAX_ID_CHARS])
def test_request_id_echoed_from_header(client, rid):
    r = client.get("/api/items/999999", headers={"X-Request-ID": rid})

    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert r.json()["request_id"] == rid


@pytest.mark.parametrize("rid", ["a b", "x" * (MAX_ID_CHARS + 1), "a/b", "id;drop", ""])
def test_bad_request_id_replaced_by_a_generated_one(client, rid):
    r = client.get("/api/items/999999", headers={"X-Request-ID": rid})

    assert GENERATED_ID.fullmatch(r.json()["request_id"])


def test_request_id_is_generated_per_request(client):
    first = client.get("/api/items/999999").json()["request_id"]
    second = client.get("/api/items/999999").json()["request_id"]

    assert GENERATED_ID.fullmatch(first) and GENERATED_ID.fullmatch(second)
    assert first != second


@pytest.mark.parametrize("sent_id", ["rid-log-1", None])
def test_request_id_in_logs_of_sync_route(client, add_route, caplog, sent_id):
    probe = logging.getLogger("tests.request_id_probe")

    def noisy() -> None:  # a sync route: FastAPI runs it in a worker thread
        probe.warning("event=probe")
        raise InvalidInputError("nope")

    add_route("/__probe", noisy)
    headers = {"X-Request-ID": sent_id} if sent_id else {}

    with caplog.at_level(logging.INFO):
        r = client.get("/__probe", headers=headers)

    assert (r.status_code, r.json()["code"]) == (400, "invalid_input")
    [record] = [rec for rec in caplog.records if rec.name == "tests.request_id_probe"]
    assert record.request_id == r.json()["request_id"] != "-"
    assert sent_id is None or record.request_id == sent_id


def test_request_id_does_not_leak_into_logs_outside_a_request(client, caplog):
    client.get("/api/items/999999", headers={"X-Request-ID": "rid-leak-1"})

    with caplog.at_level(logging.INFO):
        logging.getLogger("tests.outside").warning("event=outside")

    [record] = [rec for rec in caplog.records if rec.name == "tests.outside"]
    assert record.request_id == "-"


# --- same-origin rule (AC-46) -------------------------------------------------------------------------------


def test_cross_origin_upload_403_and_nothing_stored(client):
    r = client.post("/api/ingest", files=[_jpeg_part()], headers={"origin": "http://evil.example"})

    assert r.status_code == 403
    body = r.json()
    assert (body["code"], body["detail"]) == ("cross_origin", "Cross-origin request blocked")
    assert GENERATED_ID.fullmatch(body["request_id"])
    assert _job_count(client) == 0
    assert _original_files(client) == []


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "https://localhost",  # scheme differs
        "http://localhost:5173",  # port differs from Host "localhost"
        "http://localhost.evil.example",
        "null",  # sandboxed iframe or file://
    ],
)
def test_foreign_origin_on_upload_403_no_job(client, origin):
    r = client.post("/api/ingest", files=[_jpeg_part()], headers={"origin": origin})

    assert (r.status_code, r.json()["code"]) == (403, "cross_origin")
    assert _job_count(client) == 0


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/settings/test-llm"),
        ("PUT", "/api/profile"),
        ("PATCH", "/api/items/1"),
        ("DELETE", "/api/items/1"),
    ],
)
def test_every_unsafe_method_checks_origin(client, method, path):
    r = client.request(method, path, headers={"origin": "http://evil.example"})

    assert (r.status_code, r.json()["code"]) == (403, "cross_origin")


def test_safe_get_with_foreign_origin_proceeds_and_no_cors_headers_are_sent(client):
    r = client.get("/api/items", headers={"origin": "http://evil.example"})

    assert r.status_code == 200
    assert not [h for h in r.headers if h.lower().startswith("access-control-")]


@pytest.mark.parametrize("origin", [None, "http://localhost"])
def test_same_origin_and_originless_post_proceed(client, origin):
    profile = client.get("/api/profile").json()
    headers = {"origin": origin} if origin else {}

    r = client.put("/api/profile", json=profile, headers=headers)

    assert r.status_code == 200


def test_the_origin_must_be_the_public_url_so_dev_mode_sets_it_to_vite(settings, fake, kv):
    from kyapehnu.core.app import create_app

    app = create_app(
        settings=settings.model_copy(update={"public_url": "http://localhost:5173"}), transport=fake, kv=kv
    )
    with TestClient(app, base_url="http://localhost") as c:
        profile = c.get("/api/profile").json()
        headers = {"host": "localhost:5173"}

        vite = c.put("/api/profile", json=profile, headers={**headers, "origin": "http://localhost:5173"})
        other = c.put("/api/profile", json=profile, headers={**headers, "origin": "http://localhost"})

    assert (vite.status_code, other.status_code, other.json()["code"]) == (200, 403, "cross_origin")


# --- body cap (AC-35) ---------------------------------------------------------------------------------------


def _capped_app(kind: str):
    """A tiny app behind the guard (cap CAP bytes). Returns (asgi app, list of (content-length, bytes read))."""
    seen: list[tuple[str | None, int]] = []
    if kind == "starlette":

        async def echo(request: Request) -> JSONResponse:
            body = await request.body()
            seen.append((request.headers.get("content-length"), len(body)))
            return JSONResponse({"n": len(body)})

        inner = Starlette(routes=[Route("/echo", echo, methods=["POST"])])
    else:
        inner = FastAPI()
        install_handlers(inner)

        @inner.post("/echo")
        def echo_json(payload: dict, request: Request) -> dict:
            seen.append((request.headers.get("content-length"), len(json.dumps(payload))))
            return {"ok": True}

        @inner.post("/upload")
        def upload(files: list[UploadFile]) -> dict:
            seen.append((None, len(files)))
            return {"ok": True}

    return _guard_cap(inner), seen


def _guard_cap(inner):
    return RequestGuardMiddleware(
        inner, max_body_bytes=CAP, allowed_hosts=frozenset({"localhost"}), public_origin="http://localhost"
    )


def _chunks(data: bytes):
    yield data[:4]
    yield data[4:]


JSON_HEADERS = {"content-type": "application/json"}


@pytest.mark.parametrize("kind", ["starlette", "fastapi"])
def test_content_length_over_cap_413_and_route_not_run(kind):
    app, seen = _capped_app(kind)

    with TestClient(app, base_url="http://localhost", raise_server_exceptions=False) as c:
        r = c.post("/echo", content=BIG_BODY, headers=JSON_HEADERS)

    assert r.status_code == 413
    assert (r.json()["code"], r.json()["detail"]) == ("payload_too_large", TOO_LARGE_MESSAGE)
    assert r.json()["request_id"]
    assert seen == []


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
def test_the_upload_route_keeps_the_upload_text_when_over_the_cap(declared):
    inner = FastAPI()
    install_handlers(inner)

    @inner.post("/api/ingest")
    def ingest(payload: dict) -> dict:
        return payload

    with TestClient(_guard_cap(inner), base_url="http://localhost", raise_server_exceptions=False) as c:
        r = c.post("/api/ingest", content=BIG_BODY if declared else _chunks(BIG_BODY), headers=JSON_HEADERS)

    assert (r.status_code, r.json()["detail"]) == (413, PayloadTooLargeError.message)


@pytest.mark.parametrize("kind", ["starlette", "fastapi"])
def test_content_length_exactly_at_cap_proceeds(kind):
    app, seen = _capped_app(kind)

    with TestClient(app, base_url="http://localhost", raise_server_exceptions=False) as c:
        r = c.post("/echo", content=OK_BODY, headers=JSON_HEADERS)

    assert r.status_code == 200
    assert seen and seen[0][0] == str(CAP)


@pytest.mark.parametrize("kind", ["starlette", "fastapi"])
def test_chunked_body_over_cap_413(kind):
    """No Content-Length: the guard counts the bytes it passes on. FastAPI must not turn this into a 400."""
    app, seen = _capped_app(kind)

    with TestClient(app, base_url="http://localhost", raise_server_exceptions=False) as c:
        r = c.post("/echo", content=_chunks(BIG_BODY), headers=JSON_HEADERS)

    assert r.status_code == 413
    assert seen == []
    if kind == "fastapi":  # the handlers' envelope for a raised HTTPException (LLD 4)
        body = r.json()
        assert (body["code"], body["detail"]) == ("http_413", TOO_LARGE_MESSAGE)
        assert body["request_id"]


@pytest.mark.parametrize("kind", ["starlette", "fastapi"])
def test_chunked_body_exactly_at_cap_proceeds(kind):
    app, seen = _capped_app(kind)

    with TestClient(app, base_url="http://localhost", raise_server_exceptions=False) as c:
        r = c.post("/echo", content=_chunks(OK_BODY), headers=JSON_HEADERS)

    assert r.status_code == 200
    assert seen and seen[0][0] is None  # the precondition: the request really had no Content-Length


def test_chunked_multipart_over_cap_413_in_fastapi_form_parsing():
    app, seen = _capped_app("fastapi")
    boundary = "kyapehnuboundary"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="a.jpg"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n{'x' * 50}\r\n--{boundary}--\r\n"
    ).encode()

    with TestClient(app, base_url="http://localhost", raise_server_exceptions=False) as c:
        r = c.post(
            "/upload", content=_chunks(body), headers={"content-type": f"multipart/form-data; boundary={boundary}"}
        )

    assert r.status_code == 413
    assert seen == []
