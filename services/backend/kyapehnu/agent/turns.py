"""The lock that allows one turn at a time per chat. Lives outside `agent.chat` so `agent.public` can hand it out."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import redis
from redis.exceptions import LockError, RedisError

from kyapehnu.agent.chat.limits import TURN_LOCK_TTL_S  # a leaf module: it imports no chat service
from kyapehnu.core.errors import ServiceUnavailableError, TurnRunningError

log = logging.getLogger(__name__)


class TurnLock:
    """A Valkey lock per chat, so the turn is single even across workers. One per app, on `app.state.turns`."""

    def __init__(self, kv: redis.Redis) -> None:
        self._kv = kv

    @contextmanager
    def hold(self, chat_id: int) -> Iterator[None]:
        """Hold the chat for the length of the block. A second hold raises TurnRunningError, Valkey down raises
        ServiceUnavailableError. The key expires on its own, so a crashed backend frees the chat."""
        lock = self._kv.lock(f"turnlock:{chat_id}", timeout=TURN_LOCK_TTL_S, blocking=False)
        try:
            acquired = lock.acquire()
        except RedisError as e:
            raise ServiceUnavailableError() from e
        if not acquired:
            raise TurnRunningError()
        try:
            yield
        finally:
            try:
                lock.release()
            except LockError:
                pass  # expired during the turn, or a newer turn holds it now: nothing of ours to release
            except RedisError:
                log.warning("event=valkey_unavailable where=turn_release")  # the TTL frees it; the turn's result stands


# The one name `core.deps` types the turn lock with.
Turns = TurnLock


def make_turns(kv: redis.Redis) -> Turns:
    return TurnLock(kv)
