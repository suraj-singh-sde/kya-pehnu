"""QA: the chat stylist sees one wardrobe only (AC-19) and refuses another account's ids (AC-20, chat half).

The per-package files check the structure (a spy on the wardrobe port, the profile in the system prompt). These tests
check the content: two full wardrobes with private markers, B chats through every tool, and nothing of A's reaches the
model, a tool result, a stored row or a log line. REST suggest/swap/wear/not-worn for foreign ids are covered by
`test_isolation_wardrobe.py`.
"""

import json
import logging
from datetime import date
from typing import Any

import pytest
from sqlmodel import Session, col, select

from kyapehnu.agent.models import ChatMessage, LlmCall, ProposalState, ToolCall
from kyapehnu.wardrobe.models import Event, Item, ItemStatus, Outfit, WearLog
from tests.chat_fakes import ask, fixed_now, say
from tests.chat_helpers import DINNER, NOW, PICK, engine_of, new_chat, raw_results, send, turn
from tests.chat_rows import add_call, add_session, log_wear_ui, proposal, status_ui
from tests.fakes import add_item

A, B = 1, 2
ALPHA, BRAVO = "ALPHAMARK", "BRAVOMARK"  # appear in every private text field of A and of B


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)


def wardrobe_of(s: Session, uid: int, tag: str) -> dict[str, list[int]]:
    """Available, white, formal-3 items in three categories, named, noted and branded with the tag."""
    made: dict[str, list[int]] = {"top": [], "bottom": [], "footwear": []}
    for category in made:
        for k in range(3):
            item = add_item(
                s,
                user_id=uid,
                name=f"{tag}-{category}-{k}",
                category=category,
                brand=f"{tag}-brand",
                notes=f"{tag}-note",
                colors=[{"hex": "#ffffff", "name": "white"}],
                formality=3,
                warmth=2,
            )
            made[category].append(item.id)
    return made


def event_of(s: Session, uid: int, tag: str) -> int:
    event = Event(
        user_id=uid,
        type="party",
        title=f"{tag}-event",
        notes=f"{tag}-event-notes",
        starts_at=NOW.replace(day=10, hour=20),
        place="Gurugram",
        audience_tags=[f"{tag}-guest"],
    )
    s.add(event)
    s.commit()
    return event.id


def wear_of(s: Session, uid: int, item_ids: list[int], event_id: int) -> None:
    outfit = Outfit(user_id=uid, item_ids=item_ids, item_key=",".join(map(str, sorted(item_ids))), name=f"outfit-{uid}")
    s.add(outfit)
    s.flush()
    s.add(
        WearLog(
            user_id=uid,
            outfit_id=outfit.id,
            item_ids=item_ids,
            worn_on=date(2026, 10, 3),
            event_id=event_id,
            audience_tags=[f"{ALPHA if uid == A else BRAVO}-guest"],
        )
    )
    s.commit()


class World:
    def __init__(self, client_a, client_b):
        client_a.put("/api/profile", json={"name": ALPHA, "gender": "female", "style_prefs": f"{ALPHA}-style"})
        client_b.put("/api/profile", json={"name": BRAVO, "gender": "male", "style_prefs": f"{BRAVO}-style"})
        self.engine = engine_of(client_a)
        with Session(self.engine) as s:
            self.a_items = wardrobe_of(s, A, ALPHA)
            self.b_items = wardrobe_of(s, B, BRAVO)
            self.a_event = event_of(s, A, ALPHA)
            self.b_event = event_of(s, B, BRAVO)
            wear_of(s, A, [self.a_items["top"][0], self.a_items["bottom"][0]], self.a_event)
            wear_of(s, B, [self.b_items["top"][0], self.b_items["bottom"][0]], self.b_event)
            self.a_outfit = s.exec(select(Outfit).where(Outfit.user_id == A)).one().id
        self.a_ids = {i for ids in self.a_items.values() for i in ids}
        self.b_ids = {i for ids in self.b_items.values() for i in ids}

    def snapshot_of_a(self) -> list[dict[str, Any]]:
        with Session(self.engine) as s:
            return [
                r.model_dump(mode="json")
                for m in (Item, Event, Outfit, WearLog)
                for r in s.exec(select(m).where(col(m.user_id) == A).order_by(col(m.id)))  # type: ignore[attr-defined]
            ]


@pytest.fixture
def world(app, client_a, client_b):
    return World(client_a, client_b)


def sent_to_the_model(fake) -> str:
    return json.dumps([fake.chat_calls, fake.calls], default=lambda o: getattr(o, "__dict__", str(o)))


def stored_text(engine, chat: int) -> str:
    with Session(engine) as s:
        rows = [
            *s.exec(select(ToolCall).where(ToolCall.session_id == chat)),
            *s.exec(select(ChatMessage).where(ChatMessage.session_id == chat)),
            *s.exec(select(LlmCall).where(LlmCall.user_id == B)),
        ]
        return json.dumps([r.model_dump(mode="json") for r in rows], default=str)


def collect_item_ids(node: Any, found: set[int]) -> set[int]:
    """Every id the tool results name as an item: `item_ids` lists, and the `id` of each entry of an `items` list."""
    if isinstance(node, dict):
        found.update(i for i in node.get("item_ids") or [] if isinstance(i, int))
        for entry in node.get("items") or []:
            if isinstance(entry, dict) and isinstance(entry.get("id"), int):
                found.add(entry["id"])
        for v in node.values():
            collect_item_ids(v, found)
    elif isinstance(node, list):
        for v in node:
            collect_item_ids(v, found)
    return found


# --- AC-19: everything B's stylist is told is B's -------------------------------------------------------------------


def test_every_tool_result_and_model_request_for_b_holds_only_bs_data(world, client_b, fake, caplog):
    caplog.set_level(logging.DEBUG)
    chat = new_chat(client_b)
    b = world.b_items
    fake.replies.append(PICK)

    turn(
        client_b,
        fake,
        chat,
        ask(
            ("search_wardrobe", {}),
            ("search_wardrobe", {"q": "top"}),
            ("get_item", {"item_id": b["top"][0]}),
            ("generate_outfits", {"event_id": world.b_event}),
            ("get_wear_history", {"days": 30, "item_ids": [b["top"][0]]}),
            ("get_weather", {"place": "Gurugram"}),
            ("create_event", DINNER),
            ("log_wear", {"item_ids": [b["top"][0]]}),
        ),
        say("done"),
    )
    turn(client_b, fake, chat, text="and?")  # the turn ended on the looks: the model reads every result now

    model_saw = sent_to_the_model(fake)
    assert fake.calls, "the outfit pick request should have been made (and checked)"
    assert BRAVO in model_saw  # B's own wardrobe did reach the model
    assert ALPHA not in model_saw  # no name, note, brand, event, audience or profile text of A
    assert ALPHA not in stored_text(world.engine, chat)
    assert ALPHA not in caplog.text
    names = [json.loads(m.content) for m in raw_results(fake)]
    assert collect_item_ids(names, set()) <= world.b_ids
    assert not collect_item_ids(names, set()) & world.a_ids
    searched = names[0]
    assert searched["total"] == len(world.b_ids) == len(searched["items"])
    assert names[1]["total"] == 3  # "top" matches the three tops of B, not the three of A


def test_a_second_turn_still_holds_only_bs_data_after_a_has_chatted(world, client_a, client_b, fake):
    chat_a = new_chat(client_a)
    turn(client_a, fake, chat_a, ask(("search_wardrobe", {}), ("create_event", DINNER | {"title": f"{ALPHA} party"})))
    fake.chat_calls.clear()
    chat_b = new_chat(client_b)

    turn(client_b, fake, chat_b, ask(("search_wardrobe", {}), ("get_wear_history", {"days": 30, "item_ids": [1]})))

    assert ALPHA not in sent_to_the_model(fake)


# --- AC-19/20: B names A's ids and gets nothing, and nothing changes ------------------------------------------------


FOREIGN_CALLS = {
    "get_item": lambda w: {"item_id": w.a_items["top"][0]},
    "update_item_status": lambda w: {"item_id": w.a_items["top"][0], "status": "laundry"},
    "log_wear": lambda w: {"item_ids": [w.a_items["top"][0]]},
    "log_wear_mixed": lambda w: {"item_ids": [w.b_items["top"][0], w.a_items["top"][1]]},
    "log_wear_foreign_event": lambda w: {"item_ids": [w.b_items["top"][0]], "event_id": w.a_event},
    "generate_foreign_event": lambda w: {"event_id": w.a_event},
    "generate_include_foreign_item": lambda w: {"event_id": w.b_event, "include": [w.a_items["top"][0]]},
    "feedback_foreign_outfit": lambda w: {"outfit_id": w.a_outfit, "rating": 1},
    "feedback_foreign_items": lambda w: {"item_ids": [w.a_items["top"][0]], "rating": -1},
    "wear_history_foreign_item": lambda w: {"item_id": w.a_items["top"][0], "days": 30},
}
TOOL_OF = {
    "log_wear_mixed": "log_wear",
    "log_wear_foreign_event": "log_wear",
    "generate_foreign_event": "generate_outfits",
    "generate_include_foreign_item": "generate_outfits",
    "feedback_foreign_outfit": "record_feedback",
    "feedback_foreign_items": "record_feedback",
    "wear_history_foreign_item": "get_wear_history",
}


@pytest.mark.parametrize("case", FOREIGN_CALLS)
def test_a_tool_call_naming_as_ids_is_refused_and_changes_nothing(world, client_b, fake, case):
    before = world.snapshot_of_a()
    chat = new_chat(client_b)
    name = TOOL_OF.get(case, case)

    turn(client_b, fake, chat, ask((name, FOREIGN_CALLS[case](world))), say("ok"))

    sent = json.loads(raw_results(fake)[0].content)
    refused = "error" in sent and sent["error"]["code"] in {"not_found", "invalid_arguments"}
    nothing_found = sent.get("wears") == []  # a history of an id that is not yours is empty, not an error
    nothing_suggested = sent.get("suggestions") == [] and sent.get("problem") is not None
    assert refused or nothing_found or nothing_suggested, sent
    assert ALPHA not in json.dumps(sent) and ALPHA not in sent_to_the_model(fake)
    with Session(world.engine) as s:
        assert not s.exec(select(ToolCall).where(ToolCall.proposal_state != None)).all()  # noqa: E711  no card made
        assert s.exec(select(WearLog).where(WearLog.user_id == B, WearLog.id > 2)).all() == []
    assert world.snapshot_of_a() == before


# --- AC-20: a proposal that names A's item is refused when B presses Confirm ----------------------------------------


@pytest.fixture
def planted(world):
    """Defence in depth: B's chat holds pending proposals naming A's item (as if a tool had let one through)."""
    chat = add_session(world.engine, "B's chat", user_id=B)
    a_item = world.a_items["top"][0]
    wear = add_call(
        world.engine, chat, "log_wear", user_id=B, **proposal(log_wear_ui([a_item]), NOW.date()), call_id="w"
    )
    status = add_call(
        world.engine,
        chat,
        "update_item_status",
        user_id=B,
        **proposal(status_ui(a_item, "available", "laundry"), NOW.date()),
        call_id="s",
        step=2,
    )
    return a_item, wear, status


def test_confirming_a_proposal_that_names_as_item_is_refused_and_writes_nothing(world, client_b, planted):
    a_item, wear, status = planted
    before = world.snapshot_of_a()

    refused = [client_b.post(f"/api/chat/proposals/{pid}/confirm") for pid in (wear, status)]

    assert [r.status_code for r in refused] == [409, 409]
    assert {r.json()["code"] for r in refused} == {"proposal_expired"}
    assert world.snapshot_of_a() == before  # A's item is still available and not worn
    with Session(world.engine) as s:
        assert s.get(Item, a_item).status == ItemStatus.available
        assert s.exec(select(WearLog).where(WearLog.user_id == B, WearLog.id > 2)).all() == []
        assert {s.get(ToolCall, p).proposal_state for p in (wear, status)} == {ProposalState.expired}


def test_the_proposal_text_for_a_refused_confirm_does_not_name_as_item(world, client_b, planted):
    _a_item, wear, _status = planted

    r = client_b.post(f"/api/chat/proposals/{wear}/confirm")

    assert ALPHA not in r.text


def test_b_can_still_confirm_a_proposal_naming_their_own_item(world, client_b, fake):
    chat = new_chat(client_b)
    item = world.b_items["top"][0]
    turn(client_b, fake, chat, ask(("log_wear", {"item_ids": [item]})), say("proposed"))
    with Session(world.engine) as s:
        card = s.exec(select(ToolCall).where(ToolCall.session_id == chat, ToolCall.name == "log_wear")).one()

    r = client_b.post(f"/api/chat/proposals/{card.id}/confirm")

    assert r.status_code == 200 and r.json()["state"] == ProposalState.confirmed.value
    assert send(client_b, fake, chat, say("fine")).status_code == 200
