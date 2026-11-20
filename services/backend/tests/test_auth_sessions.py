"""Server-side sessions in Valkey (LLD 3.2 `SessionStore`; AC-7, AC-14, AC-46 unit part): the key is a hash of the
cookie, the TTL slides, a delete ends one session and no other, a dead Valkey is a 503-class error."""

import hashlib
import json
import logging
import time

import pytest
import redis

from kyapehnu.auth.sessions import SessionStore
from kyapehnu.core.errors import ServiceUnavailableError

IDLE_SECONDS = 30 * 24 * 3600


def key_of(cookie: str) -> str:
    return "session:" + hashlib.sha256(cookie.encode()).hexdigest()


@pytest.fixture
def store(kv):
    return SessionStore(kv, IDLE_SECONDS)


@pytest.fixture
def dead_store():
    dead = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, decode_responses=True)
    return SessionStore(dead, IDLE_SECONDS)


def test_a_session_is_stored_under_the_hash_of_its_cookie(store, kv):
    cookie = store.create(7)

    assert kv.keys("*") == [key_of(cookie)]


def test_no_stored_key_or_value_contains_the_cookie(store, kv):
    cookies = [store.create(7), store.create(8)]

    for key in kv.scan_iter("*"):
        for cookie in cookies:
            assert cookie not in key
            assert cookie not in kv.get(key)


def test_the_stored_value_names_the_user_and_the_creation_time_only(store, kv):
    cookie = store.create(7)

    value = json.loads(kv.get(key_of(cookie)))

    assert set(value) == {"user_id", "created_at"}
    assert value["user_id"] == 7


def test_the_cookie_is_long_random_and_never_repeats(store):
    cookies = {store.create(1) for _ in range(20)}

    assert len(cookies) == 20
    assert all(len(c) >= 43 for c in cookies)  # token_urlsafe(32): 256 bits


def test_a_new_session_lives_for_the_idle_time(store, kv):
    cookie = store.create(7)

    assert IDLE_SECONDS - 5 <= kv.ttl(key_of(cookie)) <= IDLE_SECONDS


def test_using_a_session_returns_its_user_and_slides_the_expiry(store, kv):
    cookie = store.create(7)
    kv.expire(key_of(cookie), 100)  # most of the idle time has passed

    assert store.get_and_touch(cookie) == 7

    assert IDLE_SECONDS - 5 <= kv.ttl(key_of(cookie)) <= IDLE_SECONDS


def test_an_unknown_cookie_has_no_user_and_creates_nothing(store, kv):
    assert store.get_and_touch("not-a-session") is None

    assert kv.keys("*") == []


def test_a_session_idle_past_the_expiry_is_gone(store, kv):
    cookie = store.create(7)
    kv.pexpire(key_of(cookie), 1)
    time.sleep(0.05)

    assert store.get_and_touch(cookie) is None


def test_deleting_a_session_ends_it_and_leaves_the_users_other_sessions(store):
    first, second = store.create(7), store.create(7)

    store.delete(first)

    assert store.get_and_touch(first) is None
    assert store.get_and_touch(second) == 7


def test_deleting_an_unknown_session_is_not_an_error(store, kv):
    store.delete("not-a-session")

    assert kv.keys("*") == []


@pytest.mark.parametrize("call", ["create", "get_and_touch", "delete"])
def test_a_dead_valkey_is_a_service_unavailable_error(dead_store, call, caplog):
    caplog.set_level(logging.INFO)
    args = (7,) if call == "create" else ("a-cookie",)

    with pytest.raises(ServiceUnavailableError):
        getattr(dead_store, call)(*args)

    assert "event=valkey_unavailable" in caplog.text


def test_the_unavailable_error_text_names_no_cookie(dead_store):
    with pytest.raises(ServiceUnavailableError) as raised:
        dead_store.get_and_touch("secret-cookie-value")

    assert "secret-cookie-value" not in str(raised.value)
