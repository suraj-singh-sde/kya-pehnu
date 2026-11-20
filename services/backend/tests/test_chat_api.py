"""The Phase 3 API surface that exists from the skeleton on (AC-29, AC-33, AC-34; LLD 3.1 to 3.3, 4.1, 4.2, 6).

What is tested here is what is real in WP-1: the routes are registered, behind the existing guards, `send_message` up to
the turn (422, provider, guard), the real `TurnGuard`, the schemas and their bounds, the limits and the error classes.
The service bodies behind the routes belong to the packages that fill them and are tested there.
"""

import importlib
import inspect
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session

from kyapehnu.agent.chat import limits, orchestrator
from kyapehnu.agent.chat.schemas import (
    CardSuggestion,
    MessageIn,
    MessageOut,
    OutfitsCard,
    ProposalView,
    SessionOut,
    SessionSummary,
    ToolCallView,
    TurnOut,
)
from kyapehnu.agent.models import ChatMessage, ChatSession, LlmCall, ToolCall
from kyapehnu.core import limits as core_limits
from kyapehnu.core.config import MAX_JSON_BODY_BYTES
from kyapehnu.core.errors import ConflictError, NoProviderError, ProposalExpiredError, TurnRunningError
from kyapehnu.wardrobe import items
from kyapehnu.wardrobe.events import EventDraft, EventIn
from kyapehnu.wardrobe.items import WardrobeQuery
from kyapehnu.wardrobe.models import Event, Feedback
from kyapehnu.wardrobe.outfits.schemas import (
    FeedbackIn,
    FeedbackOut,
    OutfitOut,
    Problem,
    SuggestIn,
    SuggestionOut,
    WearsQuery,
)
from kyapehnu.wardrobe.weather_report import WeatherQuery, WeatherReport
from tests.chat_fakes import fixed_now
from tests.conftest import LOOPBACK_PEER
from tests.fakes import OWNER_ID, add_item
from tests.wear_fixtures import count

NOW = datetime(2026, 10, 7, 9, 30)  # a Wednesday
LAN_IP = "192.168.1.20"
MESSAGES = "/api/chat/sessions/{}/messages"
NO_PROVIDER = "No AI provider is set up. Choose one in Settings to use the chat."
TURN_RUNNING = "A reply is still being worked on in this chat. Wait for it, then try again."


def engine_of(client):
    return client.app.state.ctx.engine


def add_session(client, title: str = "") -> int:
    with Session(engine_of(client)) as s:
        chat = ChatSession(user_id=1, title=title)
        s.add(chat)
        s.commit()
        return chat.id


def stored(client) -> dict[str, int]:
    """How many rows every table a rejected request could write holds."""
    with Session(engine_of(client)) as s:
        return {m.__name__: count(s, m) for m in (ChatSession, ChatMessage, ToolCall, Feedback, Event, LlmCall)}


def turn_out(session_id: int = 1, turn: int = 1) -> TurnOut:
    stamp = datetime(2026, 10, 7, 4, 0, tzinfo=UTC)
    summary = SessionSummary(id=session_id, title="", created_at=stamp, updated_at=stamp)
    return TurnOut(outcome="ok", turn=turn, session=SessionOut(session=summary, messages=[], tool_calls=[]))


class TurnStub:
    """Stands in for `orchestrator.run_turn`, which the chat core package writes."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, user_id, ctx, provider, session_id, text, now, clock=time.monotonic) -> TurnOut:
        self.calls.append(
            {"user_id": user_id, "ctx": ctx, "provider": provider, "session_id": session_id, "text": text, "now": now}
        )
        return turn_out(session_id)


@pytest.fixture
def run_turn(monkeypatch) -> TurnStub:
    stub = TurnStub()
    monkeypatch.setattr(orchestrator, "run_turn", stub)
    return stub


# --- the surface is there ---------------------------------------------------------------------------------------------

ROUTES = [
    ("get", "/api/chat/sessions"),
    ("post", "/api/chat/sessions"),
    ("get", "/api/chat/sessions/{session_id}"),
    ("delete", "/api/chat/sessions/{session_id}"),
    ("post", "/api/chat/sessions/{session_id}/messages"),
    ("post", "/api/chat/proposals/{tool_call_id}/confirm"),
    ("post", "/api/chat/proposals/{tool_call_id}/cancel"),
    ("post", "/api/feedback"),
    ("post", "/api/events"),
    ("get", "/api/weather"),
    ("get", "/api/wears"),
    ("get", "/api/debug/tool-calls"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_phase_3_route_is_registered(client, method, path):
    assert method in client.app.openapi()["paths"].get(path, {})


def test_the_app_holds_a_valkey_turn_lock_on_its_own_client(settings, fake, kv):
    from kyapehnu.agent.turns import TurnLock
    from kyapehnu.core.app import create_app

    app = create_app(settings=settings, transport=fake, start_worker=False, kv=kv)

    assert isinstance(app.state.turns, TurnLock)


# --- the existing guards cover every new POST and DELETE (AC-33) ------------------------------------------------------

GUARDED = [
    ("POST", "/api/chat/sessions", None, "/api/chat/sessions"),
    ("DELETE", "/api/chat/sessions/1", None, "/api/chat/sessions/{session_id}"),
    ("POST", "/api/chat/sessions/1/messages", {"text": "hi"}, "/api/chat/sessions/{session_id}/messages"),
    ("POST", "/api/chat/proposals/1/confirm", None, "/api/chat/proposals/{tool_call_id}/confirm"),
    ("POST", "/api/chat/proposals/1/cancel", None, "/api/chat/proposals/{tool_call_id}/cancel"),
    ("POST", "/api/feedback", {"item_ids": [1, 2], "rating": 1}, "/api/feedback"),
    ("POST", "/api/events", {"type": "party", "starts_at": "2026-10-10T19:00:00"}, "/api/events"),
]
GUARDED_IDS = [f"{method} {path}" for method, path, _, _ in GUARDED]


def registered(client, method: str, template: str) -> bool:
    return method.lower() in client.app.openapi()["paths"].get(template, {})


@pytest.mark.parametrize(("method", "path", "body", "template"), GUARDED, ids=GUARDED_IDS)
def test_a_foreign_origin_is_403_and_nothing_is_stored(client, method, path, body, template):
    before = stored(client)

    r = client.request(method, path, json=body, headers={"origin": "http://evil.example"})

    assert registered(client, method, template)
    assert (r.status_code, r.json()["code"]) == (403, "cross_origin")
    assert stored(client) == before


@pytest.mark.parametrize(("method", "path", "body", "template"), GUARDED, ids=GUARDED_IDS)
def test_a_foreign_host_is_400_and_nothing_is_stored(client, method, path, body, template):
    before = stored(client)

    r = client.request(method, path, json=body, headers={"host": "evil.example"})

    assert registered(client, method, template)
    assert (r.status_code, r.json()["code"]) == (400, "invalid_host")
    assert stored(client) == before


@pytest.mark.parametrize(("method", "path", "body", "template"), GUARDED, ids=GUARDED_IDS)
def test_a_body_over_the_json_cap_is_413_and_nothing_is_stored(client, method, path, body, template):
    before = stored(client)
    too_big = b'{"text": "' + b"x" * MAX_JSON_BODY_BYTES + b'"}'

    r = client.request(method, path, content=too_big, headers={"content-type": "application/json"})

    assert registered(client, method, template)
    assert (r.status_code, r.json()["code"]) == (413, "payload_too_large")
    assert stored(client) == before


def test_the_public_address_may_use_the_chat_routes(settings, fake, kv, run_turn):
    from kyapehnu.core.app import create_app

    app = create_app(
        settings=settings.model_copy(update={"public_url": f"http://{LAN_IP}:8000"}), transport=fake, kv=kv
    )

    with TestClient(app, base_url=f"http://{LAN_IP}:8000") as web:
        listed = web.get("/api/chat/sessions")
        chat = web.post("/api/chat/sessions").json()["id"]
        sent = web.post(MESSAGES.format(chat), json={"text": "hello"})

    assert listed.status_code == 200 and sent.status_code == 200


@pytest.mark.parametrize("path", ["/api/debug/tool-calls", "/api/debug/llm-calls"])
@pytest.mark.parametrize("query", ["session_id=0", "session_id=-1", "session_id=abc"])
def test_the_debug_session_filter_must_be_a_positive_integer(client, path, query):
    r = client.get(f"{path}?{query}")

    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["query", "session_id"]


@pytest.mark.parametrize("limit", [0, 1001])
def test_the_tool_call_debug_limit_is_1_to_1000(client, limit):
    r = client.get("/api/debug/tool-calls", params={"limit": limit})

    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["query", "limit"]


# --- POST /api/chat/sessions/{id}/messages: schema, provider, guard (AC-29, AC-34, AC-39) -----------------------------


def test_a_message_runs_one_turn_with_the_cleaned_text_the_session_and_the_chat_clock(client, run_turn, monkeypatch):
    fixed_now(monkeypatch, NOW)
    session_id = add_session(client)

    r = client.post(MESSAGES.format(session_id), json={"text": "  hi​ there\nsecond line \n"})

    assert r.status_code == 200
    assert (r.json()["outcome"], r.json()["turn"], r.json()["session"]["session"]["id"]) == ("ok", 1, session_id)
    (call,) = run_turn.calls
    assert call["text"] == "hi there\nsecond line"  # controls and edge whitespace go, the line break stays
    assert (call["session_id"], call["now"]) == (session_id, NOW)
    assert call["provider"] is client.app.state.ctx.provider and call["ctx"] is client.app.state.ctx


@pytest.mark.parametrize("text", ["x", "x" * 4000, "x" * 4000 + "  \n", "line\n" * 800])
def test_a_message_of_1_to_4000_characters_after_cleaning_reaches_the_turn(client, run_turn, text):
    r = client.post(MESSAGES.format(add_session(client)), json={"text": text})

    assert r.status_code == 200
    assert len(run_turn.calls) == 1 and 1 <= len(run_turn.calls[0]["text"]) <= 4000


@pytest.mark.parametrize(
    "payload",
    [{"text": ""}, {"text": "   \n\t "}, {"text": "\u200b\u200d"}, {"text": "x" * 4001}, {}, {"text": 5}],
    ids=["empty", "blank", "invisible", "4001-characters", "no-text", "not-a-string"],
)
def test_an_empty_or_over_long_message_is_422_nothing_stored_no_provider_call(client, fake, run_turn, payload):
    session_id = add_session(client)
    before = stored(client)

    r = client.post(MESSAGES.format(session_id), json=payload)

    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "text"]
    assert stored(client) == before
    assert fake.chat_calls == [] and fake.calls == [] and run_turn.calls == []


def test_no_provider_is_409_nothing_stored_no_call_and_sessions_readable(unconfigured_client, fake):
    client = unconfigured_client
    session_id = add_session(client, "Earlier chat")
    before = stored(client)

    r = client.post(MESSAGES.format(session_id), json={"text": "hello"})

    body = r.json()
    assert r.status_code == 409
    assert (body["code"], body["detail"]) == ("no_provider", NO_PROVIDER)
    assert body["request_id"]
    assert stored(client) == before
    assert fake.chat_calls == [] and fake.calls == []
    listed = client.get("/api/chat/sessions")
    assert listed.status_code == 200
    assert [(s["id"], s["title"]) for s in listed.json()] == [(session_id, "Earlier chat")]


def test_no_provider_wins_over_a_session_that_does_not_exist(unconfigured_client):
    r = unconfigured_client.post(MESSAGES.format(999), json={"text": "hello"})

    assert (r.status_code, r.json()["code"]) == (409, "no_provider")


def test_no_provider_is_reported_before_a_running_turn(unconfigured_client):
    with unconfigured_client.app.state.turns.hold(3):
        r = unconfigured_client.post(MESSAGES.format(3), json={"text": "hello"})

    assert (r.status_code, r.json()["code"]) == (409, "no_provider")


@pytest.mark.parametrize("payload", [{"text": ""}, {"text": "x" * 4001}])
def test_a_bad_message_is_422_before_the_provider_is_looked_at(unconfigured_client, payload):
    r = unconfigured_client.post(MESSAGES.format(1), json=payload)

    assert r.status_code == 422


def test_a_second_message_in_a_running_chat_is_409_turn_running_and_the_first_turn_finishes(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls: list[int] = []
    running, elsewhere = add_session(client), add_session(client)

    def blocked_turn(user_id, ctx, provider, session_id, text, now, clock=time.monotonic):
        calls.append(session_id)
        if session_id == running:
            entered.set()
            assert release.wait(timeout=10)
        return turn_out(session_id)

    monkeypatch.setattr(orchestrator, "run_turn", blocked_turn)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, MESSAGES.format(running), json={"text": "one"})
        assert entered.wait(timeout=10)

        same_chat = client.post(MESSAGES.format(running), json={"text": "two"})
        other_chat = client.post(MESSAGES.format(elsewhere), json={"text": "elsewhere"})
        release.set()
        finished = first.result(timeout=10)

    assert (same_chat.status_code, same_chat.json()["code"], same_chat.json()["detail"]) == (
        409,
        "turn_running",
        TURN_RUNNING,
    )
    assert other_chat.status_code == 200  # the guard is per chat
    assert finished.status_code == 200
    assert calls == [running, elsewhere]  # the refused message never reached the turn
    assert (
        client.post(MESSAGES.format(running), json={"text": "three"}).status_code == 200
    )  # released when the turn ended


def test_deleting_a_chat_during_its_turn_is_409_turn_running(client):
    chat = add_session(client)
    with client.app.state.turns.hold(chat):
        r = client.delete(f"/api/chat/sessions/{chat}")

    assert (r.status_code, r.json()["code"]) == (409, "turn_running")


def test_a_chat_that_is_not_the_callers_is_404_even_while_its_lock_is_held(client, run_turn):
    with client.app.state.turns.hold(9):  # no such chat for this caller: no word about the lock
        sent = client.post(MESSAGES.format(9), json={"text": "hello"})
        deleted = client.delete("/api/chat/sessions/9")

    assert [(r.status_code, r.json()["code"]) for r in (sent, deleted)] == [(404, "not_found")] * 2
    assert run_turn.calls == []


def test_the_guard_is_released_when_the_turn_fails(client, monkeypatch):
    outcomes: list = [RuntimeError("boom-detail"), "fine"]

    def flaky_turn(user_id, ctx, provider, session_id, text, now, clock=time.monotonic):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return turn_out(session_id)

    monkeypatch.setattr(orchestrator, "run_turn", flaky_turn)
    quiet = TestClient(client.app, base_url="http://localhost", client=LOOPBACK_PEER, raise_server_exceptions=False)

    chat = add_session(client)
    failed = quiet.post(MESSAGES.format(chat), json={"text": "hello"})
    again = quiet.post(MESSAGES.format(chat), json={"text": "hello"})

    assert (failed.status_code, failed.json()["code"]) == (500, "internal")
    assert "boom-detail" not in failed.text
    assert again.status_code == 200


# --- the new errors (LLD 4.1) -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (TurnRunningError, "turn_running", TURN_RUNNING),
        (NoProviderError, "no_provider", NO_PROVIDER),
        (ProposalExpiredError, "proposal_expired", None),
    ],
)
def test_the_new_errors_are_409_conflicts_with_their_codes_and_messages(error, code, message):
    assert issubclass(error, ConflictError)
    assert (error.status, error.code) == (409, code)
    if message is not None:
        assert str(error()) == message


# --- the limits (LLD 6) -----------------------------------------------------------------------------------------------

# Only the limits no other test pins with a literal number: history, result, wear, days, reason, title, model-call,
# pick and generate-run limits are pinned by test_chat_history, test_chat_limits, test_chat_tools, test_feedback and
# test_chat_store.
LIMITS = {
    "MAX_TOOL_CALLS": 12,
    "TURN_BUDGET_S": 600,
    "MAX_SEARCH_RESULTS": 50,
    "MAX_MESSAGE_CHARS": 4000,
    "SESSION_LIST_LIMIT": 500,
}


@pytest.mark.parametrize(("name", "value"), LIMITS.items())
def test_the_limit_has_its_specified_value(name, value):
    assert getattr(limits, name) == value


def test_the_wardrobe_bounds_the_chat_shares_with_the_routes_have_their_specified_values():
    assert (core_limits.MAX_INCLUDE, core_limits.MAX_EXCLUDE) == (6, 50)


def test_the_two_notices_say_what_the_design_says():
    assert limits.NO_TOOLS_NOTICE == (
        "This model cannot use tools, so it cannot see your wardrobe. Pick a tool-capable model in Settings."
    )
    assert limits.LIMIT_NOTICE == "I stopped here: this reply reached its limit of steps. Send a message to continue."


# --- request schemas (LLD 3.2) ----------------------------------------------------------------------------------------


def _fails(model, **fields) -> bool:
    try:
        model(**fields)
    except ValidationError:
        return True
    return False


def test_a_message_is_cleaned_then_held_to_1_to_4000_characters():
    assert MessageIn(text="  hello​ ").text == "hello"
    assert MessageIn(text="x" * 4000).text == "x" * 4000
    assert _fails(MessageIn, text="x" * 4001) and _fails(MessageIn, text="") and _fails(MessageIn, text=" ​ ")


@pytest.mark.parametrize(
    "fields",
    [
        {"outfit_id": 3, "rating": 1},
        {"item_ids": [2, 1], "rating": -1},
        {"outfit_id": 3, "rating": 1, "reason": "x" * 500},
    ],
)
def test_a_feedback_names_an_outfit_or_a_set_of_items_with_a_thumb(fields):
    body = FeedbackIn(**fields)

    assert body.rating in (-1, 1) and isinstance(body.reason, str)


def test_a_feedback_reason_defaults_to_empty_and_is_cleaned():
    assert FeedbackIn(outfit_id=1, rating=1).reason == ""
    assert FeedbackIn(outfit_id=1, rating=1, reason="  too​ shiny ").reason == "too shiny"


@pytest.mark.parametrize(
    "fields",
    [
        {"rating": 1},
        {"outfit_id": 1, "item_ids": [1, 2], "rating": 1},
        {"outfit_id": 1, "rating": 0},
        {"outfit_id": 1, "rating": 2},
        {"outfit_id": 1, "rating": -2},
        {"outfit_id": 1, "rating": 1, "reason": "x" * 501},
        {"outfit_id": 0, "rating": 1},
        {"item_ids": [], "rating": 1},
        {"item_ids": [1, 1], "rating": 1},
        {"item_ids": list(range(1, 12)), "rating": 1},
    ],
)
def test_an_invalid_feedback_is_refused(fields):
    assert _fails(FeedbackIn, **fields)


def test_feedback_out_carries_the_saved_rating():
    stamp = datetime(2026, 10, 7, tzinfo=UTC)

    out = FeedbackOut(id=1, outfit_id=2, rating=-1, reason="", created_at=stamp)

    assert list(FeedbackOut.model_fields) == ["id", "outfit_id", "rating", "reason", "created_at"]
    assert out.outfit_id == 2


@pytest.mark.parametrize(
    "fields", [{"item_id": 3}, {"item_ids": [1, 2]}, {"item_id": 3, "days": 1}, {"item_ids": [1], "days": 3650}]
)
def test_a_wear_history_query_names_one_item_or_one_outfit_with_days_1_to_3650(fields):
    assert not _fails(WearsQuery, **fields)


@pytest.mark.parametrize(
    "fields",
    [{}, {"item_id": 3, "item_ids": [1]}, {"item_id": 0}, {"item_id": 3, "days": 0}, {"item_id": 3, "days": 3651}],
)
def test_an_invalid_wear_history_query_is_refused(fields):
    assert _fails(WearsQuery, **fields)


def test_a_wear_history_query_looks_back_90_days_unless_told_otherwise():
    assert WearsQuery(item_id=1).days == 90


def test_a_suggest_may_force_up_to_six_distinct_items():
    assert SuggestIn(event_id=1).include == []
    assert SuggestIn(event_id=1, include=[1, 2, 3, 4, 5, 6]).include == [1, 2, 3, 4, 5, 6]
    assert _fails(SuggestIn, event_id=1, include=[1, 2, 3, 4, 5, 6, 7])
    assert _fails(SuggestIn, event_id=1, include=[1, 1])
    assert _fails(SuggestIn, event_id=1, include=[0])


def test_a_problem_may_name_a_must_include_cause():
    assert Problem(slot=None, cause="must_include", message="Item 4 is not in your wardrobe").cause == "must_include"
    assert _fails(Problem, slot=None, cause="nonsense", message="x")


@pytest.mark.parametrize("model", [SuggestionOut, OutfitOut])
def test_outfits_and_suggestions_carry_a_thumb_or_nothing_as_rating_and_it_is_optional(model):
    schema = model.model_json_schema()

    assert "rating" not in schema.get("required", [])
    assert {tuple(sorted(option.get("enum", ["null"]))) for option in schema["properties"]["rating"]["anyOf"]} == {
        (-1, 1),
        ("null",),
    }


def test_an_event_draft_is_an_event_without_a_required_place_and_with_the_same_fields_in_the_same_order():
    start = datetime(2026, 10, 10, 19)

    assert list(EventDraft.model_fields) == list(EventIn.model_fields)
    assert EventDraft(type="party", starts_at=start).place is None
    assert EventDraft(type="party", starts_at=start, place="").place == ""
    assert EventDraft(type="party", starts_at=start, place=" Pune​ ").place == "Pune"
    assert _fails(EventIn, type="party", starts_at=start, place="")  # the form's rules did not move


@pytest.mark.parametrize(
    "fields",
    [
        {"place": "p" * 121},
        {"starts_at": datetime(2026, 10, 10, 19).astimezone()},
        {"ends_at": datetime(2026, 10, 10, 18)},
        {"ends_at": datetime(2026, 10, 18, 19, 1)},
        {"type": "brunch"},
        {"audience_tags": ["x" * 41]},
    ],
)
def test_an_event_draft_keeps_the_event_rules(fields):
    assert _fails(EventDraft, **{"type": "party", "starts_at": datetime(2026, 10, 10, 19), **fields})


def test_a_weather_query_needs_nothing_and_follows_the_event_time_rules():
    q = WeatherQuery()

    assert (q.place, q.starts_at, q.ends_at, q.outdoors) == (None, None, None, False)
    assert WeatherQuery(place="Pune", starts_at=datetime(2026, 10, 10, 19), outdoors=True).outdoors is True
    for fields in (
        {"place": "p" * 121},
        {"starts_at": datetime(2026, 10, 10, 19).astimezone()},
        {"starts_at": datetime(1999, 12, 31, 23)},
        {"starts_at": datetime(2100, 1, 1)},
        {"starts_at": datetime(2026, 10, 10, 19), "ends_at": datetime(2026, 10, 10, 18)},
        {"starts_at": datetime(2026, 10, 10, 19), "ends_at": datetime(2026, 10, 18, 19, 1)},
    ):
        assert _fails(WeatherQuery, **fields), fields


# --- response schemas the frontend types are generated from (LLD 3.2) -------------------------------------------------


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (SessionSummary, {"id", "title", "created_at", "updated_at"}),
        (MessageOut, {"id", "turn", "step", "role", "content", "created_at"}),
        (CardSuggestion, {"item_ids", "items", "reason", "notes", "swap_item", "rating", "wearable"}),
        (OutfitsCard, {"event", "weather", "problem", "suggestions"}),
        (
            ProposalView,
            {
                "tool_call_id",
                "kind",
                "state",
                "proposed_on",
                "decided_at",
                "item_ids",
                "items",
                "event",
                "status_from",
                "status_to",
                "actionable",
                "result",
            },
        ),
        (ToolCallView, {"id", "turn", "step", "name", "error", "summary", "card", "event", "proposal"}),
        (SessionOut, {"session", "messages", "tool_calls"}),
        (TurnOut, {"outcome", "turn", "session"}),
        (WeatherReport, {"place", "starts_at", "ends_at", "weather"}),
        (WearsQuery, {"item_id", "item_ids", "days"}),
        (WeatherQuery, {"place", "starts_at", "ends_at", "outdoors"}),
    ],
)
def test_the_response_model_has_exactly_the_specified_fields(model, fields):
    assert set(model.model_fields) == fields


def test_a_turn_ends_with_one_of_four_outcomes_and_a_proposal_is_one_of_two_kinds():
    outcomes = TurnOut.model_json_schema()["properties"]["outcome"]["enum"]
    kinds = ProposalView.model_json_schema()["properties"]["kind"]["enum"]

    assert set(outcomes) == {"ok", "limit_reached", "provider_error", "no_tools"}
    assert set(kinds) == {"log_wear", "update_item_status"}


def test_a_card_item_may_be_missing_and_its_rating_is_a_thumb_or_nothing():
    props = CardSuggestion.model_json_schema()["properties"]

    assert {"type": "null"} in props["rating"]["anyOf"] and set(props["rating"]["anyOf"][0]["enum"]) == {-1, 1}
    assert {"type": "null"} in props["items"]["items"]["anyOf"]  # None stands for an item that was deleted


# --- GET /api/items keeps its parameters while it takes a query model (WP-Q1) -----------------------------------------


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("formality_min", 0),
        ("formality_min", 6),
        ("formality_max", 0),
        ("formality_max", 6),
        ("not_worn_days", 0),
        ("not_worn_days", 3651),
        ("season", "bogus"),
        ("category", "bogus"),
        ("status", "bogus"),
    ],
)
def test_the_item_list_still_answers_an_out_of_range_parameter_with_the_default_422_body(client, name, value):
    r = client.get("/api/items", params={name: value})

    assert r.status_code == 422
    assert [e["loc"] for e in r.json()["detail"]] == [["query", name]]


@pytest.mark.parametrize("query", ["formality_min=1&formality_max=5", "not_worn_days=1", "not_worn_days=3650"])
def test_the_item_list_accepts_the_bounds_themselves(client, query):
    assert client.get(f"/api/items?{query}").status_code == 200


def test_the_wardrobe_query_has_the_names_and_bounds_of_the_list_route():
    assert set(WardrobeQuery.model_fields) == {
        "category",
        "color",
        "formality_min",
        "formality_max",
        "season",
        "status",
        "q",
        "not_worn_days",
    }
    assert all(field.default is None for field in WardrobeQuery.model_fields.values())
    for fields in ({"formality_min": 1, "formality_max": 5}, {"not_worn_days": 3650}, {"category": "top", "q": "x"}):
        assert not _fails(WardrobeQuery, **fields)
    for fields in (
        {"formality_min": 0},
        {"formality_max": 6},
        {"not_worn_days": 0},
        {"not_worn_days": 3651},
        {"season": "bogus"},
        {"status": "bogus"},
    ):
        assert _fails(WardrobeQuery, **fields), fields


def test_a_wardrobe_query_dumps_into_the_keywords_of_list_items(engine):
    with Session(engine) as s:
        add_item(s, name="Navy shirt", category="top", formality=3)
        add_item(s, name="Chinos", category="bottom", formality=3)

        found = items.list_items(OWNER_ID, s, **WardrobeQuery(category="top").model_dump(), today=date(2026, 10, 7))

    assert [i.name for i in found] == ["Navy shirt"]


def test_q_matches_the_brand_unless_asked_not_to(engine):
    with Session(engine) as s:
        add_item(s, name="Plain shirt", brand="Zegna", tags=["linen"])

        by_default = items.list_items(OWNER_ID, s, q="zegna", today=date(2026, 10, 7))
        brand_left_out = items.list_items(OWNER_ID, s, q="zegna", brand=False, today=date(2026, 10, 7))
        name_still_found = items.list_items(OWNER_ID, s, q="plain", brand=False, today=date(2026, 10, 7))
        tag_still_found = items.list_items(OWNER_ID, s, q="LINEN", brand=False, today=date(2026, 10, 7))

    assert [len(by_default), len(brand_left_out), len(name_still_found), len(tag_still_found)] == [1, 0, 1, 1]


def test_the_rest_search_still_finds_a_brand(client):
    with Session(engine_of(client)) as s:
        add_item(s, name="Plain shirt", brand="Zegna")

    assert [i["name"] for i in client.get("/api/items", params={"q": "zegna"}).json()] == ["Plain shirt"]


# --- the signatures the later packages build on (LLD 3.3) -------------------------------------------------------------

SIGNATURES = {
    "kyapehnu.agent.chat.orchestrator:run_turn": ["user_id", "ctx", "provider", "session_id", "text", "now", "clock"],
    "kyapehnu.agent.turns:TurnLock.hold": ["self", "chat_id"],
    "kyapehnu.agent.chat.store:create_session": ["user_id", "engine"],
    "kyapehnu.agent.chat.store:list_sessions": ["user_id", "s"],
    "kyapehnu.agent.chat.store:start_turn": ["user_id", "engine", "session_id", "text"],
    "kyapehnu.agent.chat.store:add_message": ["user_id", "engine", "session_id", "turn", "step", "role", "content"],
    "kyapehnu.agent.chat.store:add_tool_call": ["engine", "row"],
    "kyapehnu.agent.chat.store:finish_turn": ["engine", "session_id"],
    "kyapehnu.agent.chat.store:session_event_ids": ["user_id", "s", "session_id"],
    "kyapehnu.agent.chat.store:session_view": ["user_id", "s", "session_id", "today"],
    "kyapehnu.agent.chat.store:proposal_view": ["user_id", "s", "tc", "today"],
    "kyapehnu.agent.chat.store:transition": ["engine", "tool_call_id", "frm", "to", "outcome"],
    "kyapehnu.agent.chat.store:set_outcome": ["engine", "tool_call_id", "outcome"],
    "kyapehnu.agent.chat.store:delete_session": ["user_id", "engine", "session_id"],
    "kyapehnu.agent.chat.proposals:confirm": ["user_id", "ctx", "tool_call_id", "today"],
    "kyapehnu.agent.chat.proposals:cancel": ["user_id", "engine", "tool_call_id", "today"],
    "kyapehnu.wardrobe.outfits.service:suggest": ["user_id", "ctx", "s", "body", "today", "view"],
    "kyapehnu.wardrobe.outfits.pick:build_prompt": [
        "event",
        "weather",
        "profile",
        "top",
        "view",
        "closest",
        "pictures",
    ],
    "kyapehnu.wardrobe.outfits.pick:choose": [
        "user_id",
        "provider",
        "ranked",
        "event",
        "weather",
        "profile",
        "swappable",
        "view",
        "closest",
        "shown",
        "pictures",
    ],
    "kyapehnu.wardrobe.outfits.candidates:apply_include": ["pool", "include", "items_by_id"],
    "kyapehnu.wardrobe.outfits.candidates:rank": ["pool", "ctx", "excluded", "per_slot", "max_candidates", "required"],
    "kyapehnu.wardrobe.outfits.wear:save_outfit": ["user_id", "ctx", "s", "ids"],
    "kyapehnu.wardrobe.outfits.wear:wear_history": ["user_id", "s", "q", "today"],
    "kyapehnu.wardrobe.outfits.wear:latest_ratings": ["user_id", "s", "outfit_ids"],
    "kyapehnu.wardrobe.outfits.wear:ratings_by_key": ["user_id", "s", "keys"],
    "kyapehnu.wardrobe.outfits.feedback:record": ["user_id", "ctx", "s", "body"],
    "kyapehnu.wardrobe.outfits.preference:load": ["user_id", "s", "items_by_id"],
    "kyapehnu.wardrobe.events:create_draft": ["user_id", "s", "draft", "home_city"],
    "kyapehnu.wardrobe.weather_report:report": ["user_id", "ctx", "s", "q", "now"],
}


def _resolve(target: str):
    module, _, dotted = target.partition(":")
    obj = importlib.import_module(module)
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


@pytest.mark.parametrize(("target", "names"), SIGNATURES.items())
def test_the_service_has_the_signature_of_the_design(target, names):
    assert list(inspect.signature(_resolve(target)).parameters) == names


@pytest.mark.parametrize(
    ("target", "name", "default"),
    [
        ("kyapehnu.wardrobe.outfits.service:suggest", "view", None),
        ("kyapehnu.wardrobe.outfits.pick:build_prompt", "view", None),
        ("kyapehnu.wardrobe.outfits.pick:choose", "view", None),
        ("kyapehnu.wardrobe.outfits.pick:choose", "closest", False),
        ("kyapehnu.wardrobe.outfits.pick:build_prompt", "closest", False),
        ("kyapehnu.wardrobe.outfits.candidates:rank", "required", frozenset()),
        ("kyapehnu.agent.chat.store:transition", "outcome", None),
        ("kyapehnu.agent.chat.orchestrator:run_turn", "clock", time.monotonic),
    ],
)
def test_the_new_parameters_have_the_defaults_that_keep_phase_2_callers_unchanged(target, name, default):
    assert inspect.signature(_resolve(target)).parameters[name].default == default


def test_the_state_transition_outcome_is_keyword_only():
    from kyapehnu.agent.chat import store

    assert inspect.signature(store.transition).parameters["outcome"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_privacy_core_names_exist():
    from kyapehnu.agent.chat import privacy

    view = privacy.ChatView(session_id=4, session_event_ids=set(), home_city="Pune")

    assert privacy.HOME_LABEL == "your home city"
    assert view.marker == "session=4"
    assert privacy.same_place("  PUNE ", "pune") and not privacy.same_place("Pune", "Mumbai")
    assert privacy.place_label("pune ", "Pune") == "your home city"
    assert privacy.place_label("Mumbai", "Pune") == "Mumbai"
