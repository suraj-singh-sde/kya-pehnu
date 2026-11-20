"""The one Valkey client of the process: sessions and sign-in rate limits (and the turn locks and caches)."""

from typing import Final

import redis

from kyapehnu.core.config import Settings

CONNECT_TIMEOUT_S: Final = 1.0
SOCKET_TIMEOUT_S: Final = 1.0
HEALTH_CHECK_INTERVAL_S: Final = 30


def make_client(settings: Settings) -> redis.Redis:
    """A client that connects on first use, so building the app needs no running Valkey."""
    password = settings.valkey_password.get_secret_value() if settings.valkey_password else None
    return redis.Redis.from_url(
        settings.valkey_url,
        password=password,
        socket_connect_timeout=CONNECT_TIMEOUT_S,
        socket_timeout=SOCKET_TIMEOUT_S,
        health_check_interval=HEALTH_CHECK_INTERVAL_S,
        decode_responses=True,
    )
