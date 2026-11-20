"""The sign-in rate limit: a moving window per client address, kept in the shared Valkey."""

import logging
import math
import time
from typing import Annotated, Final

import redis
from fastapi import Depends, Request
from limits import RateLimitItem, parse
from limits.errors import StorageError
from limits.storage import RedisStorage, Storage
from limits.strategies import MovingWindowRateLimiter

from kyapehnu.core.config import Settings
from kyapehnu.core.errors import RateLimitedError, ServiceUnavailableError

log = logging.getLogger(__name__)

NAMESPACE: Final = "auth"
UNKNOWN_ADDRESS: Final = "unknown"


class AuthRateLimiter:
    def __init__(self, item: RateLimitItem, storage: Storage) -> None:
        self._item = item
        self._window = MovingWindowRateLimiter(storage)

    def check(self, address: str) -> None:
        """Count one attempt from `address`. Raises when over the limit, or when the counts cannot be read."""
        try:
            if self._window.hit(self._item, NAMESPACE, address):
                return
            reset = self._window.get_window_stats(self._item, NAMESPACE, address).reset_time
        except (StorageError, redis.RedisError) as e:
            log.error("event=valkey_unavailable exc=%s", type(e).__name__)
            raise ServiceUnavailableError from e
        log.info("event=rate_limited")
        raise RateLimitedError(retry_after=max(1, math.ceil(reset - time.time())))


def make_limiter(settings: Settings, kv: redis.Redis, storage: Storage | None) -> AuthRateLimiter:
    """`storage` is for tests (in memory); otherwise the counts live in Valkey on the pool the sessions use."""
    if storage is None:
        storage = RedisStorage(settings.valkey_url, connection_pool=kv.connection_pool)
    return AuthRateLimiter(parse(settings.auth_rate_limit), storage)


def get_limiter(request: Request) -> AuthRateLimiter:
    limiter: AuthRateLimiter = request.app.state.limiter
    return limiter


LimiterDep = Annotated[AuthRateLimiter, Depends(get_limiter)]


def auth_rate_limit(request: Request, limiter: LimiterDep) -> None:
    """Count by the connection's address only. Behind nginx, uvicorn's proxy-headers setting has already put the real
    client there; a header sent by a browser must never decide who is counted."""
    limiter.check(request.client.host if request.client else UNKNOWN_ADDRESS)
