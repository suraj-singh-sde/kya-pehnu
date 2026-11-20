"""Wear history of one item or one outfit (AC-10; LLD 3.3): `wear.wear_history` and `GET /api/wears`.

The route counts from the real today, so the REST tests build their dates from it; the service tests pass `today`.
"""

from datetime import date, timedelta

import pytest
from sqlmodel import Session, select

from kyapehnu.core.errors import NotFoundError
from kyapehnu.wardrobe.models import Item
from kyapehnu.wardrobe.outfits import wear
from kyapehnu.wardrobe.outfits.schemas import WearsQuery
from tests.fakes import OWNER_ID, add_item, add_wear, ensure_owner


@pytest.fixture(autouse=True)
def _owner(engine):
    with Session(engine) as s:
        ensure_owner(s)
        s.commit()


TODAY = date(2026, 10, 6)


@pytest.fixture
def s(engine):
    with Session(engine) as session:
        yield session


def ids_of(s, n: int) -> list[int]:
    return [add_item(s, name=f"Item {k}").id for k in range(n)]


def days_ago(n: int, today: date = TODAY) -> date:
    return today - timedelta(days=n)


def history(s, today: date = TODAY, **query):
    return wear.wear_history(OWNER_ID, s, WearsQuery(**query), today)


# --- AC-10 ------------------------------------------------------------------------------------------------------------


def test_item_days_window_and_outfit_both_wears(client):
    today = date.today()
    with Session(client.app.state.ctx.engine) as s:
        shirt, chinos, shoes = ids_of(s, 3)
        near = add_wear(s, [shirt, shoes], days_ago(5, today))
        add_wear(s, [shirt, shoes], days_ago(40, today))  # the same outfit, outside the window
        first = add_wear(s, [chinos, shoes], days_ago(20, today))
        second = add_wear(s, [shoes, chinos], days_ago(2, today))
        near_id, first_id, second_id, second_outfit = near.id, first.id, second.id, second.outfit_id

    item = client.get("/api/wears", params={"item_id": shirt, "days": 30})
    outfit = client.get("/api/wears", params=[("item_ids", chinos), ("item_ids", shoes), ("days", 30)])

    assert item.status_code == outfit.status_code == 200
    assert [w["id"] for w in item.json()] == [near_id]
    assert [w["id"] for w in outfit.json()] == [second_id, first_id]
    assert outfit.json()[0] == {
        "id": second_id,
        "outfit_id": second_outfit,
        "item_ids": [shoes, chinos],
        "worn_on": days_ago(2, today).isoformat(),
        "event_id": None,
        "audience_tags": [],
    }


def test_an_item_in_several_outfits_has_all_their_wears_newest_first(s):
    a, b, c = ids_of(s, 3)
    old = add_wear(s, [a, b], days_ago(9))
    new = add_wear(s, [a, c], days_ago(1))
    item_only = add_wear(s, [a], days_ago(4), outfit=False)

    assert [w.id for w in history(s, item_id=a)] == [new.id, item_only.id, old.id]


def test_the_window_includes_the_day_exactly_days_ago_and_not_the_day_before(s):
    a, b = ids_of(s, 2)
    edge = add_wear(s, [a, b], days_ago(30))
    add_wear(s, [a, b], days_ago(31))

    assert [w.id for w in history(s, item_id=a, days=30)] == [edge.id]


def test_the_default_window_is_ninety_days(s):
    (a,) = ids_of(s, 1)
    inside = add_wear(s, [a], days_ago(90), outfit=False)
    add_wear(s, [a], days_ago(91), outfit=False)

    assert [w.id for w in history(s, item_id=a)] == [inside.id]


def test_at_most_a_hundred_wears_come_back_the_newest(s):
    (a,) = ids_of(s, 1)
    rows = [add_wear(s, [a], TODAY - timedelta(days=n), outfit=False) for n in range(101)]

    got = history(s, item_id=a, days=3650)

    assert [w.id for w in got] == [w.id for w in rows[:100]]


# --- an outfit is named by its items ----------------------------------------------------------------------------------


def test_an_outfit_is_found_by_its_items_in_any_order_and_item_only_wears_do_not_count(s):
    a, b = ids_of(s, 2)
    worn = add_wear(s, [a, b], days_ago(3))
    add_wear(s, [a, b], days_ago(2), outfit=False)  # worn without saving the set (AC-28 of Phase 2)

    assert [w.id for w in history(s, item_ids=[b, a])] == [worn.id]


def test_a_set_never_saved_has_no_wears_and_is_not_an_error(s):
    a, b, c = ids_of(s, 3)
    add_wear(s, [a, b], days_ago(3))

    assert history(s, item_ids=[a, c]) == []
    with pytest.raises(NotFoundError):  # a missing id looks like another user's id: neither says whether it exists
        history(s, item_ids=[a, 9999])


def test_a_subset_of_a_saved_outfit_is_another_outfit(s):
    a, b = ids_of(s, 2)
    add_wear(s, [a, b], days_ago(3))

    assert history(s, item_ids=[a]) == []


# --- unknown and deleted items ----------------------------------------------------------------------------------------


def test_an_unknown_item_is_not_found(s):
    with pytest.raises(NotFoundError):
        history(s, item_id=9999)


def test_a_wear_lists_only_the_items_that_still_exist(s):
    a, b = ids_of(s, 2)
    worn = add_wear(s, [a, b], days_ago(3))
    s.delete(s.exec(select(Item).where(Item.id == b)).one())
    s.commit()

    [got] = history(s, item_id=a)

    assert (got.id, got.item_ids) == (worn.id, [a])


# --- the route


@pytest.mark.parametrize(
    "params",
    [
        {"item_id": 1, "days": 0},
        {"item_id": 1, "days": 3651},
        {"item_id": 0},
        {},
        {"item_id": 1, "item_ids": [1, 2]},
        {"item_ids": [1, 1]},
    ],
    ids=["days 0", "days 3651", "item 0", "no subject", "both subjects", "repeated item"],
)
def test_a_bad_query_is_422(client, params):
    assert client.get("/api/wears", params=params).status_code == 422


def test_an_unknown_item_is_404_on_the_route(client):
    r = client.get("/api/wears", params={"item_id": 9999})

    assert (r.status_code, r.json()["code"]) == (404, "not_found")
