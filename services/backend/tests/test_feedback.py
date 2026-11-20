"""Thumbs on outfits (AC-2, 20, 21, 31; LLD 4.5): `POST /api/feedback` saves or reuses the outfit, never wears it.

Real SQLite and real pictures under the test data directory. No provider or weather call is made.
"""

import logging
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from kyapehnu.core.errors import NotFoundError
from kyapehnu.wardrobe.models import Feedback, Item, Outfit, WearLog
from kyapehnu.wardrobe.outfits import collage, feedback, wear
from kyapehnu.wardrobe.outfits.schemas import FeedbackIn
from tests.fakes import OWNER_ID, add_item, add_wear, ensure_owner
from tests.test_suggest_api import event_body, wardrobe
from tests.wear_fixtures import count, stored, three_items, write_picture


@pytest.fixture(autouse=True)
def _owner(engine):
    with Session(engine) as s:
        ensure_owner(s)
        s.commit()


@pytest.fixture
def ctx(client):
    return client.app.state.ctx


@pytest.fixture
def s(ctx):
    with Session(ctx.engine) as session:
        yield session


def rate(client, rating: int = 1, reason: str = "", **target):
    return client.post("/api/feedback", json={"rating": rating, "reason": reason, **target})


def fresh(ctx, model):
    with Session(ctx.engine) as other:
        return list(other.exec(select(model).order_by(model.id)).all())


# --- AC-2: an unsaved set is saved, not worn --------------------------------------------------------------------------


def test_rating_an_unsaved_set_saves_one_outfit_no_wear_and_reuses_it_for_any_order(client, ctx, s):
    a, b, c = three_items(s, ctx.data_dir)

    first = rate(client, 1, item_ids=[a.id, b.id, c.id])
    again = rate(client, 1, item_ids=[c.id, a.id, b.id])

    [outfit] = fresh(ctx, Outfit)
    assert (first.status_code, again.status_code) == (201, 201)
    assert first.json()["outfit_id"] == again.json()["outfit_id"] == outfit.id
    assert sorted(outfit.item_ids) == sorted([a.id, b.id, c.id])
    assert outfit.item_key == f"{a.id},{b.id},{c.id}"
    assert [(f.outfit_id, f.rating) for f in fresh(ctx, Feedback)] == [(outfit.id, 1), (outfit.id, 1)]
    assert fresh(ctx, WearLog) == []
    assert stored(ctx.data_dir) == [Path(outfit.collage_path).name]  # one collage, drawn once


def test_a_new_outfit_is_named_after_the_day_it_was_saved(client, ctx, s):
    a, b, _ = three_items(s, ctx.data_dir)

    rate(client, -1, item_ids=[a.id, b.id])

    today = date.today()
    assert fresh(ctx, Outfit)[0].name == f"Outfit · {today.day} {today:%b}"


def test_the_response_holds_the_feedback_row(client, ctx, s):
    a, b, _ = three_items(s, ctx.data_dir)

    body = rate(client, -1, "too shiny", item_ids=[a.id, b.id]).json()

    [row] = fresh(ctx, Feedback)
    assert body == {
        "id": row.id,
        "outfit_id": row.outfit_id,
        "rating": -1,
        "reason": "too shiny",
        "created_at": body["created_at"],
    }
    assert datetime.fromisoformat(body["created_at"]) == row.created_at


def test_rating_a_saved_outfit_by_id_draws_nothing_and_adds_no_outfit_or_wear(client, ctx, s, monkeypatch):
    a, b, _ = three_items(s, ctx.data_dir)
    worn = add_wear(s, [a.id, b.id], date(2026, 10, 1))
    monkeypatch.setattr(collage, "render", lambda *_a, **_k: pytest.fail("a collage was drawn"))

    r = rate(client, 1, outfit_id=worn.outfit_id)

    assert r.status_code == 201 and r.json()["outfit_id"] == worn.outfit_id
    assert (count(s, Outfit), count(s, WearLog)) == (1, 1)


def test_rating_a_set_that_is_already_saved_reuses_its_outfit_and_draws_nothing(client, ctx, s, monkeypatch):
    a, b, _ = three_items(s, ctx.data_dir)
    worn = add_wear(s, [a.id, b.id], date(2026, 10, 1))
    monkeypatch.setattr(collage, "render", lambda *_a, **_k: pytest.fail("a collage was drawn"))

    r = rate(client, 1, item_ids=[b.id, a.id])

    assert r.json()["outfit_id"] == worn.outfit_id
    assert count(s, Outfit) == 1


def test_the_collage_is_drawn_before_any_write_so_no_database_lock_is_held_while_drawing(client, ctx, s, monkeypatch):
    a, b, _ = three_items(s, ctx.data_dir)
    seen = {}

    def spy(_data_dir, _user_id, _items):
        with Session(ctx.engine) as other:  # a write from another connection would wait on a held lock
            seen["outfits"] = count(other, Outfit)
            add_item(other, name="Written while drawing")
        return None

    monkeypatch.setattr(collage, "render", spy)

    rate(client, 1, item_ids=[a.id, b.id])

    assert seen == {"outfits": 0}
    assert count(s, Item) == 4


# --- AC-2 races and failures: the file and the rows stay in step


def test_a_concurrent_save_of_the_same_set_is_reused_and_our_collage_is_dropped(ctx, s, monkeypatch):
    a, b, c = three_items(s, ctx.data_dir)
    winner_rel = write_picture(ctx.data_dir, "images/1/collage/winner.webp")
    ours_rel = write_picture(ctx.data_dir, "images/1/collage/ours.webp")

    def racing_render(_data_dir, _user_id, _items):  # another request saves the same set while this one draws
        with Session(ctx.engine) as other:
            ids = [a.id, b.id, c.id]
            other.add(
                Outfit(
                    user_id=OWNER_ID, item_ids=ids, item_key=wear.item_key(ids), name="Winner", collage_path=winner_rel
                )
            )
            other.commit()
        return ours_rel

    monkeypatch.setattr(collage, "render", racing_render)

    out = feedback.record(OWNER_ID, ctx, s, FeedbackIn(item_ids=[a.id, b.id, c.id], rating=1))

    [row] = fresh(ctx, Outfit)
    assert (out.outfit_id, row.name, row.collage_path) == (row.id, "Winner", winner_rel)
    assert [f.outfit_id for f in fresh(ctx, Feedback)] == [row.id]
    assert stored(ctx.data_dir) == ["winner.webp"]


def test_an_item_deleted_while_the_collage_is_drawn_is_not_found_and_nothing_stays(ctx, s, monkeypatch):
    a, b, c = three_items(s, ctx.data_dir)
    drawn_rel = write_picture(ctx.data_dir, "images/1/collage/ours.webp")

    def render_while_deleting(
        _data_dir, _user_id, _items
    ):  # the item goes after the pictures were read, before our write
        with Session(ctx.engine) as other:
            other.delete(other.get(Item, c.id))
            other.commit()
        return drawn_rel

    monkeypatch.setattr(collage, "render", render_while_deleting)

    with pytest.raises(NotFoundError, match=f"Item {c.id} not found"):
        feedback.record(OWNER_ID, ctx, s, FeedbackIn(item_ids=[a.id, b.id, c.id], rating=1))

    assert stored(ctx.data_dir) == []
    assert (len(fresh(ctx, Outfit)), len(fresh(ctx, Feedback))) == (0, 0)


def test_a_failed_commit_removes_the_collage_and_writes_nothing(ctx, s, monkeypatch):
    a, b, c = three_items(s, ctx.data_dir)

    def failing_commit(_self):
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    with monkeypatch.context() as patched:
        patched.setattr(Session, "commit", failing_commit)
        with pytest.raises(OperationalError):
            feedback.record(OWNER_ID, ctx, s, FeedbackIn(item_ids=[a.id, b.id, c.id], rating=1))

    assert stored(ctx.data_dir) == []
    assert (len(fresh(ctx, Outfit)), len(fresh(ctx, Feedback))) == (0, 0)


def test_a_failed_commit_on_a_saved_outfit_leaves_its_collage_alone(ctx, s, monkeypatch):
    a, b, _ = three_items(s, ctx.data_dir)
    kept = write_picture(ctx.data_dir, "images/1/collage/kept.webp")
    outfit = add_wear(s, [a.id, b.id], date(2026, 10, 1)).outfit_id
    s.get(Outfit, outfit).collage_path = kept
    s.commit()

    def failing_commit(_self):
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    with monkeypatch.context() as patched:
        patched.setattr(Session, "commit", failing_commit)
        with pytest.raises(OperationalError):
            feedback.record(OWNER_ID, ctx, s, FeedbackIn(item_ids=[a.id, b.id], rating=1))

    assert stored(ctx.data_dir) == ["kept.webp"]


def test_a_picture_that_cannot_be_drawn_still_saves_the_outfit_and_the_thumb(client, ctx, s, monkeypatch):
    a, b, _ = three_items(s, ctx.data_dir)
    monkeypatch.setattr(collage, "render", lambda *_a, **_k: None)

    r = rate(client, 1, item_ids=[a.id, b.id])

    assert r.status_code == 201
    assert fresh(ctx, Outfit)[0].collage_path is None


def test_the_feedback_is_logged_with_ids_and_no_reason_text(client, ctx, s, caplog):
    a, b, _ = three_items(s, ctx.data_dir)

    with caplog.at_level(logging.DEBUG):
        rate(client, -1, "REASON-MARKER-8C1", item_ids=[a.id, b.id])

    [row] = fresh(ctx, Outfit)
    assert f"event=feedback_recorded outfit_id={row.id} rating=-1" in caplog.text
    assert "REASON-MARKER-8C1" not in caplog.text


# --- AC-21: bad input writes nothing --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"item_ids": [1, 2], "rating": 0},
        {"item_ids": [1, 2], "rating": 2},
        {"item_ids": [1, 2], "rating": 1, "reason": "x" * 501},
        {"item_ids": [1, 2], "outfit_id": 1, "rating": 1},
        {"rating": 1},
    ],
    ids=["rating 0", "rating 2", "501 chars", "both targets", "no target"],
)
def test_a_bad_rating_reason_or_target_is_422_and_nothing_is_written(client, ctx, s, body):
    three_items(s, ctx.data_dir)

    r = client.post("/api/feedback", json=body)

    assert r.status_code == 422
    assert (count(s, Outfit), count(s, Feedback)) == (0, 0)
    assert stored(ctx.data_dir) == []


def test_a_reason_of_exactly_500_characters_is_accepted(client, ctx, s):
    a, b, _ = three_items(s, ctx.data_dir)

    r = rate(client, 1, "x" * 500, item_ids=[a.id, b.id])

    assert r.status_code == 201 and len(r.json()["reason"]) == 500


@pytest.mark.parametrize("target", ["outfit", "item"])
def test_an_unknown_outfit_or_item_is_404_and_nothing_is_written(client, ctx, s, target):
    a, _, _ = three_items(s, ctx.data_dir)
    fields = {"outfit_id": 9999} if target == "outfit" else {"item_ids": [a.id, 9999]}

    r = rate(client, 1, **fields)

    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert (count(s, Outfit), count(s, Feedback)) == (0, 0)
    assert stored(ctx.data_dir) == []


def test_save_outfit_names_the_unknown_item_and_draws_nothing(ctx, s):
    a, _, _ = three_items(s, ctx.data_dir)

    with pytest.raises(NotFoundError, match="Item 9999 not found"):
        wear.save_outfit(OWNER_ID, ctx, s, [a.id, 9999])

    assert stored(ctx.data_dir) == []


# --- AC-20: history is kept, the latest rating counts -----------------------------------------------------------------


def test_latest_rating_counts_history_kept(client, ctx, s):
    a, b, _ = three_items(s, ctx.data_dir)
    outfit_id = rate(client, 1, item_ids=[a.id, b.id]).json()["outfit_id"]

    rate(client, -1, "too shiny", outfit_id=outfit_id)

    assert [(f.rating, f.reason) for f in fresh(ctx, Feedback)] == [(1, ""), (-1, "too shiny")]
    assert wear.latest_ratings(OWNER_ID, s, [outfit_id]) == {outfit_id: -1}
    [saved] = client.get("/api/outfits").json()
    assert saved["rating"] == -1


# --- AC-31: a thumb shows wherever the outfit shows -----------------------------------------------------------------


def test_rating_from_outfits_page_shows_on_suggestion_saved_list_and_chat_card(client, plain_party):
    rated = plain_party()["suggestions"][0]
    assert rated["rating"] is None

    r = rate(client, 1, item_ids=rated["item_ids"])

    again = plain_party()["suggestions"]
    assert (again[0]["item_ids"], again[0]["rating"]) == (rated["item_ids"], 1)
    assert [x["rating"] for x in again[1:]] == [None, None]
    [saved] = client.get("/api/outfits").json()
    assert (saved["id"], saved["rating"]) == (r.json()["outfit_id"], 1)
    with Session(client.app.state.ctx.engine) as s:  # the chat card looks the set up by its key (LLD 5.6)
        key = wear.item_key(rated["item_ids"])
        assert wear.ratings_by_key(OWNER_ID, s, [key]) == {key: 1}


def test_a_suggestion_of_a_rated_set_carries_its_latest_rating(client, plain_party):
    only = plain_party(tops=1, bottoms=1, shoes=1)["suggestions"]
    assert [x["rating"] for x in only] == [None]
    rate(client, -1, item_ids=only[0]["item_ids"])
    rate(client, 1, item_ids=only[0]["item_ids"])

    assert [x["rating"] for x in plain_party()["suggestions"]] == [1]


def test_swap_carries_the_rating_of_the_outfit_it_returns(client, s):
    made = wardrobe(s, tops=2, bottoms=1, shoes=1)
    event = client.post("/api/outfits/suggest", json={"event": event_body()}).json()["event"]
    current = [made["top"][0].id, made["bottom"][0].id, made["footwear"][0].id]
    swapped = [made["top"][1].id, *current[1:]]
    rate(client, -1, item_ids=swapped)

    out = client.post(
        "/api/outfits/swap", json={"event_id": event["id"], "item_ids": current, "item_id": current[0]}
    ).json()

    assert sorted(out["item_ids"]) == sorted(swapped) and out["rating"] == -1


@pytest.fixture
def plain_party(client, s, monkeypatch):
    """Suggest for a party with no AI provider; the wardrobe is built on first use."""
    monkeypatch.setattr(client.app.state.ctx, "settings", None)
    built: list[object] = []

    def run(tops: int = 3, bottoms: int = 3, shoes: int = 3) -> dict:
        if not built:
            built.append(wardrobe(s, tops=tops, bottoms=bottoms, shoes=shoes))
        return client.post("/api/outfits/suggest", json={"event": event_body()}).json()

    return run
