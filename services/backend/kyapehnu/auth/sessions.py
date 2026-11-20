"""Server-side sessions in Valkey. The cookie is a random token; Valkey only ever sees its hash."""

import hashlib
import json
import logging
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final, cast

import redis

from kyapehnu.core.db import utcnow
from kyapehnu.core.errors import ServiceUnavailableError

log = logging.getLogger(__name__)

KEY_PREFIX: Final = "session:"
TOKEN_BYTES: Final = 32


def _key(cookie: str) -> str:
    return KEY_PREFIX + hashlib.sha256(cookie.encode()).hexdigest()


@contextmanager
def _valkey_errors() -> Iterator[None]:
    try:
        yield
    except redis.RedisError as e:
        log.error("event=valkey_unavailable exc=%s", type(e).__name__)
        raise ServiceUnavailableError from e


class SessionStore:
    def __init__(self, kv: redis.Redis, idle_seconds: int) -> None:
        self._kv = kv
        self._idle = idle_seconds

    def create(self, user_id: int) -> str:
        cookie = secrets.token_urlsafe(TOKEN_BYTES)
        value = json.dumps({"user_id": user_id, "created_at": utcnow().isoformat()})
        with _valkey_errors():
            self._kv.set(_key(cookie), value, ex=self._idle)
        return cookie

    def get_and_touch(self, cookie: str) -> int | None:
        """The user the session belongs to, or None when it is unknown or idle too long. Every use slides the expiry."""
        with _valkey_errors():
            value = cast(str | None, self._kv.getex(_key(cookie), ex=self._idle))
        return None if value is None else int(json.loads(value)["user_id"])

    def delete(self, cookie: str) -> None:
        with _valkey_errors():
            self._kv.delete(_key(cookie))
