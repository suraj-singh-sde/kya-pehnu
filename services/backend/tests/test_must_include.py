"""Must-include (AC-47; LLD 5.5): `include` puts items into every outfit, or names the item or the clash that stops it.

The unit tests build pools in memory; the API tests run `POST /api/outfits/suggest` with no provider (the fallback
ranking) or with a scripted one.
"""

import pytest
from sqlmodel import Session

from kyapehnu.core.config import WeatherPrefs
from kyapehnu.wardrobe.outfits.candidates import Pool, apply_include, build_pool, rank
from kyapehnu.wardrobe.outfits.schemas import Problem
from kyapehnu.wardrobe.outfits.slots import Slot
from tests.fakes import add_item
from tests.outfit_fixtures import item, make_ctx
from tests.test_suggest_api import event_body, pick_reply, prompt_candidates, shown, wardrobe

FORMALITY = (2, 4)


def world(*items):
    """The pool of the items for a formality 2-4 event with no weather, and the by-id map apply_include is given."""
    pool, problem = build_pool(list(items), FORMALITY, None, WeatherPrefs())
    return pool, problem, {i.id: i for i in items}


def kit(**extra):
    """Two tops, two bottoms, a dress, a jacket, two shoes and a bag: every slot, ids 1 to 9."""
    return [
        item(1, "top", name="Shirt", formality=3),
        item(2, "top", name="Tee", formality=3),
        item(3, "bottom", name="Chinos", formality=3),
        item(4, "bottom", name="Jeans", formality=3),
        item(5, "dress", name="Dress", formality=3),
        item(6, "outerwear", name="Jacket", formality=3),
        item(7, "footwear", name="Loafers", formality=3),
        item(8, "footwear", name="Boots", formality=3),
        item(9, "accessory", name="Belt", formality=3),
    ]


def ids(pool: Pool) -> dict[Slot, list[int]]:
    return {slot: [i.id for i in items] for slot, items in pool.by_slot.items()}


# --- apply_include: the pool ------------------------------------------------------------------------------------------


def test_an_included_item_outside_the_dress_level_or_weather_is_kept():
    """The wearer chose it: the dress level and the weather filter the rest of the look, not the piece they named."""
    pool, _, by_id = world(*kit()[:5], item(6, "outerwear", name="Jacket", formality=5), *kit()[6:])
    assert ids(pool)[Slot.outerwear] == []

    reduced, required, problem = apply_include(pool, [6], by_id)

    assert problem is None and required == {Slot.outerwear} and ids(reduced)[Slot.outerwear] == [6]


def test_each_included_slot_is_reduced_to_the_item_and_the_others_are_kept():
    pool, _, by_id = world(*kit())

    reduced, required, problem = apply_include(pool, [6, 7], by_id)

    assert problem is None
    assert required == {Slot.outerwear, Slot.footwear}
    assert ids(reduced) == {**ids(pool), Slot.outerwear: [6], Slot.footwear: [7]}
    assert reduced.notes == pool.notes


def test_a_top_or_bottom_include_empties_the_dresses_and_a_dress_include_empties_tops_and_bottoms():
    pool, _, by_id = world(*kit())

    with_top, _, _ = apply_include(pool, [1], by_id)
    with_bottom, _, _ = apply_include(pool, [4], by_id)
    with_dress, required, _ = apply_include(pool, [5], by_id)

    assert (ids(with_top)[Slot.top], ids(with_top)[Slot.bottom], ids(with_top)[Slot.full]) == ([1], [3, 4], [])
    assert (ids(with_bottom)[Slot.top], ids(with_bottom)[Slot.bottom], ids(with_bottom)[Slot.full]) == ([1, 2], [4], [])
    assert (ids(with_dress)[Slot.top], ids(with_dress)[Slot.bottom], ids(with_dress)[Slot.full]) == ([], [], [5])
    assert required == {Slot.full}


def test_a_top_and_a_bottom_together_leave_no_dress_and_require_both_slots():
    pool, _, by_id = world(*kit())

    reduced, required, problem = apply_include(pool, [2, 3], by_id)

    assert problem is None and required == {Slot.top, Slot.bottom}
    assert (ids(reduced)[Slot.top], ids(reduced)[Slot.bottom], ids(reduced)[Slot.full]) == ([2], [3], [])


def test_an_empty_include_changes_nothing():
    pool, _, by_id = world(*kit())

    same, required, problem = apply_include(pool, [], by_id)

    assert (ids(same), required, problem) == (ids(pool), frozenset(), None)


def test_the_pool_it_is_given_is_not_changed():
    pool, _, by_id = world(*kit())
    before = ids(pool)

    apply_include(pool, [1, 6], by_id)

    assert ids(pool) == before


# --- apply_include: the named problems (AC-47) -----------------------------------------------------------------------


def laundered():
    return [*kit()[:8], item(9, "accessory", name="Belt", status="laundry")]


@pytest.mark.parametrize(
    ("include", "wardrobe_items", "slot", "message"),
    [
        ([77], kit(), None, "Item 77 is not in your wardrobe"),
        ([9], laundered(), Slot.accessory, "Belt is not available (laundry)"),
        ([3, 4], kit(), Slot.bottom, "Chinos and Jeans are both bottoms: an outfit holds one"),
        ([7, 8], kit(), Slot.footwear, "Loafers and Boots are both footwears: an outfit holds one"),
        ([5, 1], kit(), Slot.top, "Dress is a full outfit and cannot go with Shirt"),
        ([1, 5], kit(), Slot.full, "Dress is a full outfit and cannot go with Shirt"),
        ([5, 3], kit(), Slot.bottom, "Dress is a full outfit and cannot go with Chinos"),
    ],
    ids=["unknown", "laundry", "two bottoms", "two shoes", "top then dress", "dress then top", "bottom"],
)
def test_problem_names_the_item_or_the_clash(include, wardrobe_items, slot, message):
    pool, _, by_id = world(*wardrobe_items)

    reduced, required, problem = apply_include(pool, include, by_id)

    assert problem == Problem(slot=slot, cause="must_include", message=message)
    assert (ids(reduced), required) == (ids(pool), frozenset())  # nothing is reduced when a problem is returned


def test_a_retired_item_is_named_as_not_available():
    items = [*kit(), item(10, "top", name="Old tee", status="retired")]
    pool, _, by_id = world(*[i for i in items if i.status != "retired"])  # the service never pools a retired item

    _, _, problem = apply_include(pool, [10], {i.id: i for i in items})

    assert problem is not None and problem.message == "Old tee is not available (retired)"


def test_the_first_problem_in_the_order_given_wins():
    pool, _, by_id = world(*laundered())

    assert apply_include(pool, [77, 9], by_id)[2].message == "Item 77 is not in your wardrobe"
    assert apply_include(pool, [9, 77], by_id)[2].message == "Belt is not available (laundry)"
    assert apply_include(pool, [1, 2, 77], by_id)[2].message == "Shirt and Tee are both tops: an outfit holds one"


def test_a_top_that_has_no_bottom_to_go_with_is_named_not_left_to_an_empty_result():
    only_dress_and_top = [item(1, "top", name="Shirt", formality=3), item(5, "dress", name="Dress", formality=3)]
    pool, problem, by_id = world(*only_dress_and_top)
    assert problem is None  # the dress alone makes outfits

    _, _, got = apply_include(pool, [1], by_id)

    assert got == Problem(
        slot=Slot.bottom, cause="must_include", message="Shirt has no bottom to go with it: none fits this event"
    )


# --- rank(required=)


def _ranked(pool: Pool, required=frozenset()):
    return rank(pool, make_ctx(formality=FORMALITY), set(), 4, 1000, required)


def test_outerwear_is_optional_without_required_and_always_there_with_it():
    pool, _, _ = world(*kit())
    jacket_only = Pool({**pool.by_slot, Slot.outerwear: [kit()[5]]}, pool.notes)

    free = _ranked(jacket_only)
    required = _ranked(jacket_only, frozenset({Slot.outerwear}))

    assert any(6 not in c.item_ids for c in free)
    assert required and all(6 in c.item_ids for c in required)
    assert len(required) < len(free)


def test_footwear_and_accessory_are_never_dropped_when_required():
    pool, _, by_id = world(*kit())
    reduced, required, _ = apply_include(pool, [7, 9], by_id)

    got = _ranked(reduced, required)

    assert got and all({7, 9} <= set(c.item_ids) for c in got)


def test_a_required_dress_is_in_every_outfit_and_no_top_or_bottom_comes_with_it():
    pool, _, by_id = world(*kit())
    reduced, required, _ = apply_include(pool, [5], by_id)

    got = _ranked(reduced, required)

    assert got and all(c.item_ids[0] == 5 and not {1, 2, 3, 4} & set(c.item_ids) for c in got)


def test_required_defaults_to_nothing_so_the_phase_2_ranking_is_unchanged():
    pool, _, _ = world(*kit())

    assert [c.item_ids for c in _ranked(pool)] == [c.item_ids for c in _ranked(pool, frozenset())]


# --- through the API (AC-47)


@pytest.fixture
def cool(fake_weather):
    """A mild evening (feels like 20 °C): the default bands then allow an optional layer."""
    fake_weather.values["apparent_temperature"] = lambda t: 20.0


@pytest.fixture
def db(client):
    with Session(client.app.state.ctx.engine) as s:
        yield s


@pytest.fixture
def plain(client, monkeypatch):
    monkeypatch.setattr(client.app.state.ctx, "settings", None)
    return client


def suggest(client, **body):
    return client.post("/api/outfits/suggest", json={"event": event_body(), **body})


def with_jackets(db):
    made = wardrobe(db)
    red = add_item(db, name="Red jacket", category="outerwear", formality=3, warmth=3, thumb_path="images/thumb/j.webp")
    blue = add_item(db, name="Blue jacket", category="outerwear", formality=3, warmth=3)
    return made, red, blue


def test_every_outfit_has_the_jacket_and_differs_from_excluded(plain, db, cool):
    _, red, _ = with_jackets(db)
    assert any(red.id not in ids_ for ids_ in shown(suggest(plain).json()))  # without include the jacket is optional

    first = suggest(plain, include=[red.id]).json()
    again = suggest(plain, include=[red.id], exclude=shown(first)).json()

    assert (len(first["suggestions"]), first["problem"]) == (3, None)
    assert len(again["suggestions"]) == 3
    assert all(red.id in ids_ for ids_ in shown(first) + shown(again))
    assert not {frozenset(x) for x in shown(first)} & {frozenset(x) for x in shown(again)}


def test_the_candidates_the_model_is_shown_all_hold_the_included_item(client, db, cool, fake):
    _, red, _ = with_jackets(db)
    fake.replies = [pick_reply((1, "one"), (2, "two"), (3, "three"))]

    r = suggest(client, include=[red.id])

    candidates = prompt_candidates(fake.calls[0]["prompt"])
    assert len(candidates) > 3 and all(red.id in members for members in candidates.values())
    assert all(red.id in ids_ for ids_ in shown(r.json()))


def test_an_included_piece_outside_the_dress_level_is_in_every_outfit(plain, db, cool):
    with_jackets(db)
    black_tie = add_item(db, name="Tuxedo jacket", category="outerwear", formality=5, warmth=3)

    body = suggest(plain, include=[black_tie.id]).json()

    assert body["problem"] is None and body["suggestions"]
    assert all(black_tie.id in ids_ for ids_ in shown(body))


def test_include_with_a_dress_gives_only_dress_outfits(plain, db, cool):
    wardrobe(db)
    dress = add_item(db, name="Red dress", category="dress", formality=3, warmth=2)

    body = suggest(plain, include=[dress.id]).json()

    assert body["suggestions"] and all(dress.id in ids_ and len(ids_) <= 3 for ids_ in shown(body))
    assert all(x["items"][0]["category"] == "dress" for x in body["suggestions"])


@pytest.mark.parametrize("case", ["laundry", "two bottoms", "full with top", "retired"])
def test_problem_names_the_item_through_the_api(plain, db, cool, case):
    made, *_ = with_jackets(db)
    bottoms, tops = made["bottom"], made["top"]
    laundry = add_item(db, name="Green jacket", category="outerwear", formality=3, warmth=3, status="laundry")
    retired = add_item(db, name="Old jacket", category="outerwear", formality=3, warmth=3, status="retired")
    dress = add_item(db, name="Red dress", category="dress", formality=3, warmth=2)
    include, expected = {
        "laundry": ([laundry.id], "Green jacket is not available (laundry)"),
        "two bottoms": (
            [bottoms[0].id, bottoms[1].id],
            f"{bottoms[0].name} and {bottoms[1].name} are both bottoms: an outfit holds one",
        ),
        "full with top": ([dress.id, tops[0].id], f"{dress.name} is a full outfit and cannot go with {tops[0].name}"),
        "retired": ([retired.id], "Old jacket is not available (retired)"),
    }[case]

    r = suggest(plain, include=include)

    body = r.json()
    assert r.status_code == 200 and body["suggestions"] == []
    assert body["problem"] == {"slot": body["problem"]["slot"], "cause": "must_include", "message": expected}


def test_an_include_id_that_is_not_in_the_wardrobe_is_a_404_not_a_problem(plain, db, cool):
    """An id that does not exist looks like another user's id: neither says whether the item exists (AC-20)."""
    with_jackets(db)

    r = suggest(plain, include=[9999])

    assert (r.status_code, r.json()["code"]) == (404, "not_found")


def test_an_event_problem_comes_before_the_include_check(plain, db, cool):
    top = add_item(db, name="Shirt", category="top", formality=3)

    body = suggest(plain, include=[top.id]).json()

    assert body["problem"]["cause"] == "missing" and body["suggestions"] == []


def test_no_include_and_an_empty_include_give_the_same_suggestions(plain, db, cool):
    with_jackets(db)

    absent = suggest(plain).json()
    empty = suggest(plain, include=[]).json()

    assert shown(absent) == shown(empty) and absent["problem"] is None


def test_an_include_that_leaves_nothing_new_is_exhausted_not_must_include(plain, db, cool):
    made = wardrobe(db, tops=1, bottoms=1, shoes=1)
    only = [made["top"][0].id, made["bottom"][0].id, made["footwear"][0].id]

    body = suggest(plain, include=[only[0]], exclude=[only]).json()

    assert body["suggestions"] == [] and body["problem"]["cause"] == "exhausted"


def test_a_saved_event_takes_include_too(client, db, cool, fake):
    _, red, _ = with_jackets(db)
    event_id = suggest(client).json()["event"]["id"]
    fake.replies = [pick_reply((1, "one"))]

    r = client.post("/api/outfits/suggest", json={"event_id": event_id, "include": [red.id]})

    assert len(shown(r.json())) == 1 and all(red.id in ids_ for ids_ in shown(r.json()))  # the model's one pick
    assert all(red.id in members for members in prompt_candidates(fake.calls[-1]["prompt"]).values())


def test_a_model_hint_to_swap_the_included_jacket_is_dropped_and_a_hint_for_another_slot_stays(client, db, cool, fake):
    made, red, blue = with_jackets(db)
    fake.replies = [pick_reply((1, "one"))]
    suggest(client, include=[red.id])  # the same request shows which items candidate 2 holds
    second_holds = prompt_candidates(fake.calls[0]["prompt"])[2]
    spare_top = next(t for t in made["top"] if t.id not in second_holds)
    fake.replies = [pick_reply((1, "one", "outerwear", blue.id), (2, "two", "top", spare_top.id), (3, "three"))]

    r = suggest(client, include=[red.id])

    first, second, third = r.json()["suggestions"]
    assert all(red.id in s["item_ids"] for s in (first, second, third))
    assert first["swap"] is None  # the jacket was asked for: no hint may replace it
    assert second["swap"] is not None and (second["swap"]["slot"], second["swap"]["item_id"]) == ("top", spare_top.id)


# --- a saree and its blouse ------------------------------------------------------------------------------------------


def saree_kit():
    return [
        item(11, "ethnic", name="Silk saree", subcategory="saree", formality=3),
        item(12, "ethnic", name="Saree blouse", subcategory="blouse", formality=3),
        item(13, "ethnic", name="Lehenga", subcategory="lehenga", formality=3),
        *kit(),
    ]


def test_including_a_blouse_keeps_only_the_sarees_and_lehengas():
    pool, _, by_id = world(*saree_kit())

    reduced, required, problem = apply_include(pool, [12], by_id)

    assert problem is None and required == {Slot.blouse}
    assert (ids(reduced)[Slot.top], ids(reduced)[Slot.bottom], ids(reduced)[Slot.full]) == ([], [], [11, 13])


def test_including_a_dress_leaves_no_blouse_and_a_saree_keeps_it():
    pool, _, by_id = world(*saree_kit())

    with_dress, _, _ = apply_include(pool, [5], by_id)
    with_saree, _, _ = apply_include(pool, [11], by_id)

    assert ids(with_dress)[Slot.blouse] == [] and ids(with_saree)[Slot.blouse] == [12]


@pytest.mark.parametrize(
    ("include", "slot", "message"),
    [
        ([12, 1], Slot.top, "Saree blouse only goes with a saree or lehenga, not with Shirt"),
        ([1, 12], Slot.blouse, "Saree blouse only goes with a saree or lehenga, not with Shirt"),
        ([5, 12], Slot.blouse, "Saree blouse only goes with a saree or lehenga, not with Dress"),
        ([12, 5], Slot.full, "Saree blouse only goes with a saree or lehenga, not with Dress"),
    ],
)
def test_a_blouse_clashes_with_a_top_a_bottom_or_a_dress(include, slot, message):
    pool, _, by_id = world(*saree_kit())

    _, _, problem = apply_include(pool, include, by_id)

    assert problem == Problem(slot=slot, cause="must_include", message=message)


def test_a_saree_whose_blouse_is_in_the_laundry_is_named_for_the_blouse_not_the_weather():
    items = saree_kit()
    items[1].status = "laundry"
    pool, _, by_id = world(*items)

    _, _, problem = apply_include(pool, [11], by_id)

    assert problem is not None and problem.slot is Slot.full
    assert problem.message == "Silk saree has no blouse to go with it"
