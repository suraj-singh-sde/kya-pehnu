"""Application errors and the single mapping from exceptions to HTTP responses."""

import errno
import logging
import sqlite3
from contextvars import ContextVar
from typing import ClassVar
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException

log = logging.getLogger(__name__)

# Lives here so this module imports no app module; the request guard sets it per request.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

DB_BUSY_MARKERS = ("locked", "busy")
DISK_FULL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})
SQLITE_PRIMARY_CODE_MASK = 0xFF  # the low byte of an extended result code is the primary one
RETRY_AFTER_SECONDS = 1
INTERNAL_DETAIL = "Internal error"


class AppError(Exception):
    status: ClassVar[int] = 500
    code: ClassVar[str] = "internal"
    message: ClassVar[str] = INTERNAL_DETAIL

    def __init__(self, message: str | None = None) -> None:
        super().__init__(self.message if message is None else message)

    def headers(self) -> dict[str, str]:
        """Extra response headers (Retry-After on a retryable error)."""
        return {}


class InvalidInputError(AppError, ValueError):
    status = 400
    code = "invalid_input"
    message = "Invalid input"


class UnsupportedFileError(InvalidInputError):
    code = "unsupported_file"
    message = "unsupported file type"


class HeicExtraMissingError(InvalidInputError):
    code = "heic_extra_missing"
    message = "HEIC photos are not supported. Export them as JPEG."


class ImageTooLargeError(InvalidInputError):
    code = "image_too_large"
    message = "image too large (pixel limit 89.5 MP)"


class NotAuthenticatedError(AppError):
    status = 401
    code = "not_authenticated"
    message = "Sign in to continue"


class AuthNotConfiguredError(NotAuthenticatedError):
    code = "auth_not_configured"
    message = "Sign-in is not configured on this Kya Pehnu"


class PermissionDeniedError(AppError):
    status = 403
    code = "permission_denied"
    message = "Only the admin can do this"


class RateLimitedError(AppError):
    status = 429
    code = "rate_limited"
    message = "Too many sign-in attempts. Try again shortly."

    def __init__(self, retry_after: int, message: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)}


class NotFoundError(AppError):
    status = 404
    code = "not_found"
    message = "Not found"


class ConflictError(AppError):
    status = 409
    code = "conflict"
    message = "Conflict"


class ReviewError(ConflictError):
    code = "review_conflict"
    message = "piece is not pending review"


class PayloadTooLargeError(AppError):
    status = 413
    code = "payload_too_large"
    message = "Upload too large: max 200 files, 25 MB each"


class PlaceNotFoundError(AppError):
    status = 422
    code = "place_not_found"
    message = "Place not found. Check the spelling or try a nearby city."


class ItemNotInOutfitError(AppError):
    status = 422
    code = "item_not_in_outfit"
    message = "The item to swap is not in this outfit"


class NoImageModelError(ConflictError):
    code = "no_image_model"
    message = "Pictures on a mannequin need OpenAI as the AI provider (Settings)"


class TurnRunningError(ConflictError):
    code = "turn_running"
    message = "A reply is still being worked on in this chat. Wait for it, then try again."


class NoProviderError(ConflictError):
    code = "no_provider"
    message = "No AI provider is set up. Choose one in Settings to use the chat."


class ProposalExpiredError(ConflictError):
    """The proposal can no longer be carried out; the message names why (`Not done: <reason>. Nothing was changed.`)."""

    code = "proposal_expired"
    message = "Not done: the proposal is out of date. Nothing was changed."


class NoAlternativeError(ConflictError):
    code = "no_alternative"
    message = "No other item fits this slot"


class UnavailableError(AppError):
    status = 503
    code = "db_busy"
    message = "Database is busy, retry shortly"

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(RETRY_AFTER_SECONDS)}


class ServiceUnavailableError(UnavailableError):
    code = "service_unavailable"
    message = "A required service is unavailable. Retry shortly."


class GoogleUnreachableError(ServiceUnavailableError):
    code = "google_unreachable"
    message = "Sign-in provider unreachable. Check the internet connection and retry."


class ProviderError(AppError):
    """The AI provider failed or returned something unusable for a request the user is waiting on."""

    status = 502
    code = "provider_error"
    message = "The AI provider could not complete the request"


class InsufficientStorageError(AppError):
    status = 507
    code = "insufficient_storage"
    message = "Disk is full"


class StartupRefusedError(Exception):
    """The database on disk is not one this version may open. Not an HTTP error: the process exits (CLI code 2)."""


class PreOwnershipDatabaseError(StartupRefusedError):
    """A Kya Pehnu Phase 3 database: it has no owner column and must be imported."""


class UnknownDatabaseError(StartupRefusedError):
    """A file that is not a Kya Pehnu database, or one of a revision this version does not know."""


class ImportRefusedError(Exception):
    """A command-line import or restore that stopped before changing anything. The text never carries an email."""


def error_body(detail: str, code: str) -> dict[str, str]:
    return {"detail": detail, "code": code, "request_id": request_id_var.get()}


def _app_error_response(exc: AppError) -> JSONResponse:
    return JSONResponse(error_body(str(exc), exc.code), status_code=exc.status, headers=exc.headers())


def _internal_response() -> JSONResponse:
    return JSONResponse(error_body(INTERNAL_DETAIL, AppError.code), status_code=AppError.status)


def _disk_full_response() -> JSONResponse:
    body = error_body(InsufficientStorageError.message, InsufficientStorageError.code)
    return JSONResponse(body, status_code=InsufficientStorageError.status)


def _os_error_response(exc: OSError) -> JSONResponse:
    if isinstance(exc, ConnectionError):  # the client went away mid-request: nothing to report
        log.debug("event=client_disconnected", exc_info=exc)
        return _internal_response()
    if exc.errno in DISK_FULL_ERRNOS:
        log.error("event=disk_full errno=%s", exc.errno, exc_info=exc)
        return _disk_full_response()
    log.error("event=os_error errno=%s", exc.errno, exc_info=exc)
    return _internal_response()


def _is_sqlite_full(exc: OperationalError) -> bool:
    code = getattr(exc.orig, "sqlite_errorcode", None)
    return code is not None and code & SQLITE_PRIMARY_CODE_MASK == sqlite3.SQLITE_FULL


def install_handlers(app: FastAPI) -> None:
    """Register the only exception-to-response mapping. Bodies never carry exception text of unexpected errors."""

    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        return _app_error_response(exc)

    @app.exception_handler(OperationalError)
    async def _operational_error(_request: Request, exc: OperationalError) -> JSONResponse:
        if any(marker in str(exc.orig).lower() for marker in DB_BUSY_MARKERS):
            return _app_error_response(UnavailableError())
        if _is_sqlite_full(exc):
            log.error("event=disk_full", exc_info=exc)
            return _disk_full_response()
        log.error("event=db_error", exc_info=exc)
        return _internal_response()

    @app.exception_handler(OSError)
    async def _os_error(_request: Request, exc: OSError) -> JSONResponse:
        return _os_error_response(exc)

    @app.exception_handler(HTTPException)
    async def _http_exception(_request: Request, exc: HTTPException) -> JSONResponse:
        # FastAPI wraps any error raised while reading the body (a full temp disk) into a 400.
        if isinstance(exc.__cause__, OSError):
            return _os_error_response(exc.__cause__)
        body = error_body(exc.detail, f"http_{exc.status_code}")
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # No traceback here: Starlette re-raises after this response and uvicorn prints it once.
        log.error("event=unhandled_error exc=%s path=%s", type(exc).__name__, quote(request.url.path))
        return _internal_response()
