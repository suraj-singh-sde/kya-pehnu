"""The `kv` fixture: a fresh fake per test, a real Valkey only for tests marked `integration`."""

import fakeredis
import pytest


def test_the_default_kv_is_a_fake_and_starts_empty(kv):
    assert isinstance(kv, fakeredis.FakeRedis)
    assert kv.keys("*") == []
    kv.set("only-in-this-test", "1")


def test_the_fake_is_not_shared_between_tests(kv):
    assert kv.get("only-in-this-test") is None


@pytest.mark.integration
def test_an_integration_test_gets_a_real_empty_valkey(kv):
    assert not isinstance(kv, fakeredis.FakeRedis)
    assert kv.ping() is True
    assert kv.keys("*") == []
