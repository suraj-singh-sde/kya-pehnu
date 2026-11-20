"""AC-44: after a full flow, every key in Valkey belongs to one of the five allowed kinds (integration: real Valkey)."""

import pytest

from tests.qa_flow import ALLOWED_PREFIXES, run_full_flow

pytestmark = pytest.mark.integration


def test_after_a_full_flow_every_valkey_key_is_one_of_the_five_kinds(
    make_app, google, fake, fake_weather, kv, monkeypatch
):
    flow = run_full_flow(make_app, google, fake, fake_weather, kv, monkeypatch)

    keys = set(kv.scan_iter("*")) | flow.keys_in_turn

    assert keys, "the flow left nothing in Valkey: the check would pass for the wrong reason"
    assert {k for k in keys if not k.startswith(ALLOWED_PREFIXES)} == set()
    for prefix in ALLOWED_PREFIXES:  # and each kind was really produced by the flow
        assert any(k.startswith(prefix) for k in keys), prefix


def test_no_key_outlives_its_purpose_without_a_ttl(make_app, google, fake, fake_weather, kv, monkeypatch):
    run_full_flow(make_app, google, fake, fake_weather, kv, monkeypatch)

    forever = [k for k in kv.scan_iter("*") if kv.ttl(k) == -1]

    assert forever == []  # every kind expires: Valkey can be flushed or filled without growing for ever
