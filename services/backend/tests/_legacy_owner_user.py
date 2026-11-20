"""Test-only (work plan 0.1): lets the tests written before accounts existed keep their one implicit owner.

Owner 1 is the account with the same Google id as the `sign_in(app, "sub-a")` account of the `client` fixture. Every
`TestClient` of a legacy test module carries that person's session. Nothing stamps rows any more: the services and the
tests name the owner. No production code depends on any of it."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

import kyapehnu.user.profile
from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.core.db import utcnow
from kyapehnu.core.errors import ServiceUnavailableError
from kyapehnu.user.public import ensure_profile

LEGACY_OWNER_ID = 1
LEGACY_OWNER_SUB = "sub-a"
SESSION_COOKIE = "kyapehnu_session"

# `PROFILE_ID` was the row id of the one profile. The first account's profile row has the same id.
kyapehnu.user.profile.PROFILE_ID = 1  # type: ignore[attr-defined]

# The test modules that existed before accounts did. A package that ports one to the sign-in fixtures removes it here.
LEGACY_MODULES = frozenset(
    {
        "test_api",
        "test_chat_api",
        "test_chat_concurrency",
        "test_chat_debug",
        "test_chat_events",
        "test_chat_fakes",
        "test_chat_history",
        "test_chat_limits",
        "test_chat_logging",
        "test_chat_migration",
        "test_chat_no_tools",
        "test_chat_perf",
        "test_chat_privacy",
        "test_chat_prompt",
        "test_chat_proposals",
        "test_chat_safety",
        "test_chat_sessions",
        "test_chat_store",
        "test_chat_tools",
        "test_chat_turn",
        "test_cleanup",
        "test_cleanup_phase3",
        "test_cli",
        "test_color",
        "test_config_db",
        "test_constraints",
        "test_domain_boundaries",
        "test_errors",
        "test_events_create",
        "test_fakes",
        "test_feedback",
        "test_fix_round_1",
        "test_get_owned",
        "test_health",
        "test_housekeeping",
        "test_images",
        "test_kv_fixture",
        "test_live",
        "test_llm",
        "test_llm_chat",
        "test_llm_prefix",
        "test_llm_resolve",
        "test_llm_settings",
        "test_llm_store",
        "test_mannequin",
        "test_middleware",
        "test_must_include",
        "test_outfit_rules",
        "test_outfits_api",
        "test_outfits_perf",
        "test_pick",
        "test_pictures",
        "test_pipeline",
        "test_preference",
        "test_qa_gaps",
        "test_qa_phase2",
        "test_qa_phase3",
        "test_repo_layout",
        "test_review_items",
        "test_scoring",
        "test_security_fixes",
        "test_seed_backup",
        "test_settings",
        "test_suggest_api",
        "test_tagging_dedupe",
        "test_text_types",
        "test_vision_check",
        "test_wear",
        "test_wear_history",
        "test_weather",
        "test_weather_report",
        "test_weather_summary",
    }
)


def install_legacy_sign_in(module_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """In a legacy test module every `TestClient` built on a Kya Pehnu app carries the legacy owner's session."""
    if module_name.rsplit(".", 1)[-1] not in LEGACY_MODULES:
        return
    original = TestClient.__init__

    def init(self: TestClient, app: object, *args: object, **kwargs: object) -> None:
        original(self, app, *args, **kwargs)  # type: ignore[arg-type]
        state = getattr(app, "state", None)
        if hasattr(state, "sessions") and SESSION_COOKIE not in self.cookies:
            ctx = state.ctx
            with Session(ctx.engine) as s:
                user = upsert_google_user(
                    s,
                    sub=LEGACY_OWNER_SUB,
                    email=f"{LEGACY_OWNER_SUB}@example.com",
                    name=LEGACY_OWNER_SUB,
                    now=utcnow(),
                )
                user_id = int(user.id)
                ensure_profile(user_id, s, ctx.prefs)
            try:
                self.cookies.set(SESSION_COOKIE, state.sessions.create(user_id))
            except ServiceUnavailableError:  # a test that kills Valkey on purpose has no session to give
                pass

    monkeypatch.setattr(TestClient, "__init__", init)
