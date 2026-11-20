"""Pure ASGI request guard: request id, Host allowlist, same-origin rule for unsafe methods, request body cap."""

import logging
import re
from typing import Final
from urllib.parse import quote, urlsplit
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kyapehnu.core.config import MAX_JSON_BODY_BYTES
from kyapehnu.core.errors import PayloadTooLargeError, error_body, request_id_var

log = logging.getLogger(__name__)

UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REQUEST_ID_RE: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")  # fullmatch, else generated: no log injection
REQUEST_ID_HEADER: Final = "X-Request-ID"
UPLOAD_ROUTE: Final = ("POST", "/api/ingest")  # the only route that takes a large body
TOO_LARGE_MESSAGE: Final = "Request too large"
NOSNIFF_HEADER: Final = "X-Content-Type-Options"
HOST_FORBIDDEN_CHARS: Final = frozenset("@/?#\\")


def host_name(host_header: str) -> str | None:
    """The lowercased host without port or brackets, or None when the header is not a plain host[:port]."""
    if HOST_FORBIDDEN_CHARS.intersection(host_header):
        return None
    try:
        return urlsplit(f"//{host_header}").hostname
    except ValueError:
        return None


def _request_id(headers: Headers) -> str:
    sent = headers.get(REQUEST_ID_HEADER, "")
    return sent if REQUEST_ID_RE.fullmatch(sent) else uuid4().hex


def _declared_length(headers: Headers) -> int | None:
    try:
        return int(headers["content-length"])
    except (KeyError, ValueError):
        return None


def _is_upload(scope: Scope) -> bool:
    return (scope["method"], scope["path"]) == UPLOAD_ROUTE


def _json_error(status: int, detail: str, code: str) -> Response:
    return JSONResponse(error_body(detail, code), status_code=status)


class RequestGuardMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int, allowed_hosts: frozenset[str], public_origin: str) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.allowed_hosts = allowed_hosts
        self.public_origin = public_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        request_id = _request_id(headers)
        # Not reset afterwards: the error handlers of an exception that escapes this guard still need it, and
        # every request runs in its own task, so it cannot leak into another request.
        request_id_var.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers[REQUEST_ID_HEADER] = request_id
                response_headers[NOSNIFF_HEADER] = "nosniff"  # uploaded originals are served by extension only
            await send(message)

        limit = self._body_limit(scope)
        rejection = self._rejection(scope, headers, limit)
        if rejection is not None:
            await rejection(scope, receive, send_with_id)
            return
        await self.app(scope, self._capped(receive, limit, self._too_large_message(scope)), send_with_id)

    @staticmethod
    def _too_large_message(scope: Scope) -> str:
        """The upload text names files and megabytes: only the upload route can have exceeded those."""
        return PayloadTooLargeError.message if _is_upload(scope) else TOO_LARGE_MESSAGE

    def _body_limit(self, scope: Scope) -> int:
        # By route, never by the client's Content-Type: a JSON route reads its whole body into memory.
        if _is_upload(scope):
            return self.max_body_bytes
        return min(self.max_body_bytes, MAX_JSON_BODY_BYTES)

    def _rejection(self, scope: Scope, headers: Headers, limit: int) -> Response | None:
        host = headers.get("host", "")
        if host_name(host) not in self.allowed_hosts:
            log.warning("event=host_rejected")  # the value is attacker-controlled and stays out of the log
            return _json_error(400, "Invalid host header", "invalid_host")
        origin = headers.get("origin")
        if scope["method"] in UNSAFE_METHODS and origin is not None and origin != self.public_origin:
            log.warning("event=origin_rejected method=%s path=%s", scope["method"], quote(scope["path"]))
            return _json_error(403, "Cross-origin request blocked", "cross_origin")
        declared = _declared_length(headers)
        if declared is not None and declared > limit:
            log.warning("event=body_rejected declared=%d", declared)
            return _json_error(PayloadTooLargeError.status, self._too_large_message(scope), PayloadTooLargeError.code)
        return None

    @staticmethod
    def _capped(receive: Receive, limit: int, too_large: str) -> Receive:
        """Count the bytes the app actually reads, so a body without Content-Length cannot exceed the cap."""
        received = 0

        async def capped() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(PayloadTooLargeError.status, too_large)
            return message

        return capped
