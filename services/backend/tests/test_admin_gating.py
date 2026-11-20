"""AI settings and Debug are for the admin (AC-11, FR-21; Cleanup C-8): B gets 403 `permission_denied` on every write
and on Debug, A works, and a refused call changes and calls nothing."""

import pytest

from kyapehnu.agent.llm.base import LLMProvider
from tests.chat_helpers import engine_of, llm_rows

CHOICE = {"provider": "openai", "api_key": "sk-test-key-0000000000", "model": "gpt-x"}
ADMIN_ONLY = [
    ("post", "/api/settings/llm/models", {"provider": "openai", "api_key": "sk-test-key-0000000000"}),
    ("post", "/api/settings/llm/test", CHOICE),
    ("put", "/api/settings/llm", CHOICE),
    ("post", "/api/settings/test-llm", None),
    ("get", "/api/debug/llm-calls", None),
    ("get", "/api/debug/tool-calls", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_ONLY, ids=[f"{m} {p}" for m, p, _ in ADMIN_ONLY])
def test_b_is_refused_with_403_and_nothing_is_called_or_changed(app, fake, client_a, client_b, method, path, body):
    before = (list(fake.calls), list(fake.chat_calls), len(llm_rows(engine_of(client_a))))

    r = getattr(client_b, method)(path, **({} if body is None else {"json": body}))

    assert (r.status_code, r.json()["code"]) == (403, "permission_denied")
    assert (list(fake.calls), list(fake.chat_calls), len(llm_rows(engine_of(client_a)))) == before


@pytest.mark.parametrize(("method", "path"), [("get", "/api/debug/llm-calls"), ("get", "/api/debug/tool-calls")])
def test_a_reaches_debug(client_a, method, path):
    assert getattr(client_a, method)(path).status_code == 200


def test_the_connectivity_check_logs_its_call_under_the_admins_id(app, fake, client_a, monkeypatch):
    seen: list[tuple[str, int | None]] = []
    original = LLMProvider.complete_json

    def spy(self, **kwargs):
        seen.append((kwargs["purpose"], kwargs.get("user_id")))
        return original(self, **kwargs)

    monkeypatch.setattr(LLMProvider, "complete_json", spy)
    fake.replies.append({"ok": True})

    r = client_a.post("/api/settings/test-llm")

    assert (r.status_code, r.json()["ok"]) == (200, True)
    assert seen == [("test", 1)]


def test_the_settings_page_tells_each_person_what_they_may_edit(app, client_a, client_b):
    mine, theirs = client_a.get("/api/settings"), client_b.get("/api/settings")

    assert (mine.status_code, theirs.status_code) == (200, 200)
    assert (mine.json()["is_admin"], theirs.json()["is_admin"]) == (True, False)
    assert mine.json()["llm"]["can_edit"] is True or mine.json()["llm"]["managed_by_env"] is True
    assert theirs.json()["llm"]["can_edit"] is False
    assert theirs.json()["llm"]["base_url"] is None and theirs.json()["llm"]["key_last4"] is None
