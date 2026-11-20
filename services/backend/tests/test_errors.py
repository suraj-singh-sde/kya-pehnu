import errno
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException

from kyapehnu.core.errors import (
    AppError,
    AuthNotConfiguredError,
    ConflictError,
    GoogleUnreachableError,
    HeicExtraMissingError,
    ImageTooLargeError,
    InsufficientStorageError,
    InvalidInputError,
    ItemNotInOutfitError,
    NoAlternativeError,
    NotAuthenticatedError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    PlaceNotFoundError,
    RateLimitedError,
    ReviewError,
    ServiceUnavailableError,
    UnavailableError,
    UnsupportedFileError,
    error_body,
    install_handlers,
    request_id_var,
)

REQUEST_ID = "rid-test"

HIERARCHY = [
    (AppError, 500, "internal", "Internal error"),
    (InvalidInputError, 400, "invalid_input", "Invalid input"),
    (UnsupportedFileError, 400, "unsupported_file", "unsupported file type"),
    (
        HeicExtraMissingError,
        400,
        "heic_extra_missing",
        "HEIC photos are not supported. Export them as JPEG.",
    ),
    (ImageTooLargeError, 400, "image_too_large", "image too large (pixel limit 89.5 MP)"),
    (NotAuthenticatedError, 401, "not_authenticated", "Sign in to continue"),
    (AuthNotConfiguredError, 401, "auth_not_configured", "Sign-in is not configured on this Kya Pehnu"),
    (PermissionDeniedError, 403, "permission_denied", "Only the admin can do this"),
    (NotFoundError, 404, "not_found", "Not found"),
    (ConflictError, 409, "conflict", "Conflict"),
    (ReviewError, 409, "review_conflict", "piece is not pending review"),
    (PayloadTooLargeError, 413, "payload_too_large", "Upload too large: max 200 files, 25 MB each"),
    (UnavailableError, 503, "db_busy", "Database is busy, retry shortly"),
    (ServiceUnavailableError, 503, "service_unavailable", "A required service is unavailable. Retry shortly."),
    (
        GoogleUnreachableError,
        503,
        "google_unreachable",
        "Sign-in provider unreachable. Check the internet connection and retry.",
    ),
    (InsufficientStorageError, 507, "insufficient_storage", "Disk is full"),
    (
        PlaceNotFoundError,
        422,
        "place_not_found",
        "Place not found. Check the spelling or try a nearby city.",
    ),
    (ItemNotInOutfitError, 422, "item_not_in_outfit", "The item to swap is not in this outfit"),
    (NoAlternativeError, 409, "no_alternative", "No other item fits this slot"),
]


def _raiser(exc: BaseException):
    def route() -> None:
        raise exc

    return route


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    install_handlers(app)

    @app.middleware("http")
    async def set_request_id(request, call_next):
        request_id_var.set(REQUEST_ID)
        return await call_next(request)

    routes = {
        "locked": OperationalError("x", {}, Exception("database is locked")),
        "busy": OperationalError("x", {}, Exception("database table is busy")),
        "other-operational": OperationalError("x", {}, Exception("no such table: secret_table")),
        "enospc": OSError(errno.ENOSPC, "full"),
        "edquot": OSError(errno.EDQUOT, "quota"),
        "eacces": OSError(errno.EACCES, "x"),
        "file-missing": FileNotFoundError("/private/path/x.jpg"),
        "keyerror": KeyError("secret"),
        "http-413": HTTPException(413, "Too big"),
    }
    for name, exc in routes.items():
        app.get(f"/{name}")(_raiser(exc))
    for cls, *_ in HIERARCHY:
        app.get(f"/app/{cls.__name__}")(_raiser(cls()))
    app.get("/rate-limited")(_raiser(RateLimitedError(retry_after=17)))
    app.get("/custom")(_raiser(NotFoundError("Item 7 not found")))
    app.get("/custom-no-alternative")(_raiser(NoAlternativeError("No other top fits this event")))

    @app.get("/typed/{number}")
    def typed(number: int) -> int:
        return number

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _assert_envelope(resp, status: int, code: str) -> dict:
    body = resp.json()
    assert resp.status_code == status
    assert set(body) == {"detail", "code", "request_id"}
    assert body["code"] == code
    assert body["request_id"] == REQUEST_ID
    return body


@pytest.mark.parametrize(("cls", "status", "code", "message"), HIERARCHY, ids=[h[0].__name__ for h in HIERARCHY])
def test_app_error_maps_to_status_code_and_default_message(client, cls, status, code, message):
    body = _assert_envelope(client.get(f"/app/{cls.__name__}"), status, code)
    assert body["detail"] == message


def test_custom_message_overrides_default(client):
    body = _assert_envelope(client.get("/custom"), 404, "not_found")
    assert body["detail"] == "Item 7 not found"


def test_no_alternative_is_a_conflict_and_keeps_its_slot_message(client):
    assert issubclass(NoAlternativeError, ConflictError)

    body = _assert_envelope(client.get("/custom-no-alternative"), 409, "no_alternative")

    assert body["detail"] == "No other top fits this event"


def test_empty_message_is_kept_not_replaced_by_default():
    assert str(NotFoundError("")) == ""


def test_error_body_uses_request_id_var():
    assert error_body("d", "c") == {"detail": "d", "code": "c", "request_id": "-"}
    token = request_id_var.set("abc")
    try:
        assert error_body("d", "c")["request_id"] == "abc"
    finally:
        request_id_var.reset(token)


def test_unavailable_error_503_has_retry_after(client):
    resp = client.get("/app/UnavailableError")
    assert resp.headers["retry-after"] == "1"


@pytest.mark.parametrize("cls", [ServiceUnavailableError, GoogleUnreachableError])
def test_unavailable_subclasses_keep_the_retry_after_header(client, cls):
    assert client.get(f"/app/{cls.__name__}").headers["retry-after"] == "1"


def test_rate_limited_is_429_with_the_wait_in_retry_after(client):
    resp = client.get("/rate-limited")

    body = _assert_envelope(resp, 429, "rate_limited")
    assert body["detail"] == "Too many sign-in attempts. Try again shortly."
    assert resp.headers["retry-after"] == "17"


@pytest.mark.parametrize("cls", [NotAuthenticatedError, PermissionDeniedError, NotFoundError])
def test_other_errors_send_no_retry_after(client, cls):
    assert "retry-after" not in client.get(f"/app/{cls.__name__}").headers


def test_auth_not_configured_is_a_not_authenticated_error():
    assert issubclass(AuthNotConfiguredError, NotAuthenticatedError)


@pytest.mark.parametrize("route", ["locked", "busy"])
def test_db_locked_maps_to_503(client, route):
    resp = client.get(f"/{route}")
    body = _assert_envelope(resp, 503, "db_busy")
    assert body["detail"] == "Database is busy, retry shortly"
    assert resp.headers["retry-after"] == "1"


def test_other_operational_error_500_without_internals(client, caplog):
    with caplog.at_level(logging.ERROR):
        resp = client.get("/other-operational")
    body = _assert_envelope(resp, 500, "internal")
    assert "secret_table" not in resp.text
    assert body["detail"] == "Internal error"
    assert any(r.exc_info for r in caplog.records)


@pytest.mark.parametrize("route", ["enospc", "edquot"])
def test_disk_full_maps_to_507(client, route):
    body = _assert_envelope(client.get(f"/{route}"), 507, "insufficient_storage")
    assert body["detail"] == "Disk is full"


@pytest.mark.parametrize("route", ["eacces", "file-missing"])
def test_other_oserror_500_internal(client, route, caplog):
    with caplog.at_level(logging.ERROR):
        resp = client.get(f"/{route}")
    body = _assert_envelope(resp, 500, "internal")
    assert body["detail"] == "Internal error"
    assert "/private/path" not in resp.text
    assert any(r.exc_info for r in caplog.records)


def test_unexpected_error_500_envelope(client):
    resp = client.get("/keyerror")
    body = _assert_envelope(resp, 500, "internal")
    assert body["detail"] == "Internal error"
    assert "secret" not in resp.text
    assert "KeyError" not in resp.text


def test_unexpected_error_logged_without_traceback(client, caplog):
    with caplog.at_level(logging.ERROR):
        client.get("/keyerror")
    records = [r for r in caplog.records if "event=unhandled_error" in r.getMessage()]
    assert len(records) == 1
    assert "exc=KeyError" in records[0].getMessage()
    assert "path=/keyerror" in records[0].getMessage()
    assert records[0].exc_info is None
    assert "secret" not in records[0].getMessage()


def test_http_exception_uses_envelope_with_http_code(client):
    body = _assert_envelope(client.get("/http-413"), 413, "http_413")
    assert body["detail"] == "Too big"


def test_unknown_route_404_uses_envelope(client):
    body = _assert_envelope(client.get("/does-not-exist"), 404, "http_404")
    assert body["detail"] == "Not Found"


def test_validation_error_keeps_fastapi_422_shape(client):
    resp = client.get("/typed/abc")
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], list)


def test_handlers_leave_success_untouched(client):
    resp = client.get("/typed/5")
    assert resp.status_code == 200
    assert resp.json() == 5
