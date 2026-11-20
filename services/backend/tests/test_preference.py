"""The preference part of the score (AC-22, AC-25; LLD 5.5, 5.6, ADR-0014): tags, the seventh weight, the neutral value.

WP-1 holds the pure part and the ratings queries. The learned values (`load` over feedback rows) are tested where they
are filled in.
"""

import math
from datetime import date, timedelta

import pytest
from sqlalchemy import event
from sqlmodel import Session, select

from kyapehnu.core.config import WeatherPrefs, Weights
from kyapehnu.wardrobe.models import Feedback, Item, Outfit
from kyapehnu.wardrobe.outfits import wear
from kyapehnu.wardrobe.outfits.candidates import build_pool, rank
from kyapehnu.wardrobe.outfits.preference import NEUTRAL, TANH_SCALE, TagPreference, item_tags, load
from kyapehnu.wardrobe.outfits.repetition import History
from kyapehnu.wardrobe.outfits.schemas import Parts
from kyapehnu.wardrobe.outfits.scoring import ScoreContext, item_prescore, score, weighted_total
from kyapehnu.wardrobe.outfits.slots import Slot
from kyapehnu.wardrobe.seed import SEED_ITEMS
from tests.fakes import OWNER_ID, add_item, add_wear, ensure_owner
from tests.outfit_fixtures import TODAY, item, make_ctx


@pytest.fixture(autouse=True)
def _owner(engine):
    with Session(engine) as s:
        ensure_owner(s)
        s.commit()


SIX = ("colour", "pattern", "formality", "weather", "repetition", "forgotten")


def outfit_of(*items: Item) -> list[tuple[Slot, Item]]:
    slots = (Slot.top, Slot.bottom, Slot.footwear)
    return list(zip(slots, items, strict=False))


# --- item_tags (FR-13, AC-49) -----------------------------------------------------------------------------------------


def test_item_tags_are_colour_names_pattern_fabric_category_subcategory_and_user_tags_trimmed_and_lower_case():
    shirt = item(
        1,
        colors=[{"hex": "#000080", "name": "Navy"}, {"hex": "#f5f5dc", "name": " BEIGE "}],
        pattern="Stripe",
        fabric="Linen",
        category="top",
        subcategory="Shirt",
        tags=["  Silky ", "WORK"],
    )

    assert item_tags(shirt) == frozenset({"navy", "beige", "stripe", "linen", "top", "shirt", "silky", "work"})


def test_item_tags_leave_out_empty_values_so_no_empty_tag_ever_exists():
    plain = item(1, colors=[], pattern="solid", fabric=None, subcategory="", tags=["", "  "])

    assert item_tags(plain) == frozenset({"solid", "top"})


def test_two_items_share_a_tag_whatever_its_letter_case():
    assert "silky" in item_tags(item(1, tags=["Silky"])) & item_tags(item(2, tags=["silky"]))


# --- TagPreference.part -----------------------------------------------------------------------------------------------


def test_the_part_is_one_half_plus_half_the_tanh_of_the_net_over_the_scale():
    shirt = item(1, colors=[{"hex": "#000080", "name": "navy"}])
    preference = TagPreference({"navy": -2.0, "unrelated": 5.0})

    assert preference.part([shirt]) == pytest.approx(0.5 + 0.5 * math.tanh(-2.0 / TANH_SCALE))


def test_the_tanh_scale_is_four():
    assert TANH_SCALE == 4.0


def test_a_tag_two_items_share_counts_once():
    shirt = item(1, colors=[{"hex": "#000080", "name": "navy"}])
    chinos = item(2, "bottom", colors=[{"hex": "#000080", "name": "navy"}])

    assert TagPreference({"navy": -4.0}).part([shirt, chinos]) == pytest.approx(0.5 + 0.5 * math.tanh(-1.0))


def test_tags_with_no_net_value_leave_the_part_at_one_half():
    assert TagPreference({"red": 3.0}).part([item(1, colors=[{"hex": "#000080", "name": "navy"}])]) == 0.5


def test_the_part_never_rises_as_down_ratings_pile_up_and_stays_in_the_unit_range():
    shirt = item(1, colors=[{"hex": "#000080", "name": "navy"}])
    parts = [TagPreference({"navy": -float(n)}).part([shirt]) for n in (0, 1, 2, 5, 20, 1000, 10**9)]

    assert parts == sorted(parts, reverse=True)
    assert all(0.0 <= p <= 1.0 for p in parts)


def test_the_part_never_falls_as_up_ratings_pile_up_and_stays_in_the_unit_range():
    shirt = item(1, colors=[{"hex": "#000080", "name": "navy"}])
    parts = [TagPreference({"navy": float(n)}).part([shirt]) for n in (0, 1, 2, 5, 20, 1000, 10**9)]

    assert parts == sorted(parts)
    assert all(0.0 <= p <= 1.0 for p in parts)


# --- neutral (AC-22) --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "items",
    [
        [item(1)],
        [item(1, pattern="stripe", tags=["silky"]), item(2, "bottom"), item(3, "footwear", fabric="leather")],
        [item(1, colors=[{"hex": "#ff0000", "name": "red"}], subcategory="kurta"), item(2, "bottom")],
    ],
)
def test_the_neutral_preference_scores_every_outfit_one_half(items):
    assert NEUTRAL.part(items) == 0.5


def test_no_feedback_is_neutral(engine):
    with Session(engine) as s:
        made = [
            add_item(s, name="Shirt", category="top", tags=["silky"]),
            add_item(s, name="Chinos", category="bottom"),
        ]
        by_id = {i.id: i for i in made}

        preference = load(OWNER_ID, s, by_id)

    assert preference.part(made) == 0.5
    assert preference.part(made[:1]) == 0.5


def test_score_gives_preference_one_half_when_the_context_has_no_learned_preference():
    _, parts = score(outfit_of(item(1), item(2, "bottom"), item(3, "footwear")), make_ctx())

    assert parts.preference == 0.5


def test_a_context_built_the_phase_2_way_gets_the_neutral_preference():
    assert make_ctx().preference is NEUTRAL


def test_score_takes_the_preference_part_from_the_context():
    liked = item(1, colors=[{"hex": "#000080", "name": "navy"}])
    other = item(1, colors=[{"hex": "#ff0000", "name": "red"}])
    ctx = make_ctx(preference=TagPreference({"navy": -8.0}))

    _, disliked_parts = score(outfit_of(liked, item(2, "bottom"), item(3, "footwear")), ctx)
    _, other_parts = score(outfit_of(other, item(2, "bottom"), item(3, "footwear")), ctx)

    assert disliked_parts.preference == pytest.approx(0.5 + 0.5 * math.tanh(-2.0))
    assert other_parts.preference == 0.5


def test_the_context_field_comes_last_with_a_default_so_old_constructors_still_work():
    fields = list(ScoreContext.__dataclass_fields__)

    assert fields[-1] == "preference"
    assert ScoreContext.__dataclass_fields__["preference"].default is NEUTRAL


# --- the seventh weight (AC-25) ---------------------------------------------------------------------------------------


def test_seven_weights_normalised():
    w = Weights()

    values = [getattr(w, name) for name in (*SIX, "preference")]
    assert sum(values) == pytest.approx(1.0)
    assert w.preference == pytest.approx(0.10 / 1.10)
    assert values[:6] == pytest.approx([v / 1.10 for v in (0.25, 0.10, 0.20, 0.20, 0.20, 0.05)])


def test_a_preference_weight_of_zero_gives_back_the_phase_2_six_weights():
    w = Weights(preference=0)

    assert [getattr(w, name) for name in SIX] == pytest.approx([0.25, 0.10, 0.20, 0.20, 0.20, 0.05])
    assert w.preference == 0


def test_a_larger_preference_weight_takes_a_larger_share_of_the_total():
    w = Weights(preference=1.0)  # the six default weights sum to 1, so preference is half of 2

    assert (w.preference, w.colour) == (pytest.approx(0.5), pytest.approx(0.125))


def test_the_weighted_total_adds_the_preference_part_times_its_weight():
    parts = Parts(colour=0, pattern=0, formality=0, weather=0, repetition=0, forgotten=0, preference=0.8)

    assert weighted_total(
        parts, Weights(colour=0, pattern=0, formality=0, weather=0, repetition=0, forgotten=0, preference=1)
    ) == (pytest.approx(0.8))
    assert weighted_total(parts, Weights()) == pytest.approx(0.10 * 0.8 / 1.10)


def test_parts_require_a_preference_so_the_api_contract_names_all_seven():
    with pytest.raises(ValueError, match="preference"):
        Parts(colour=1, pattern=1, formality=1, weather=1, repetition=1, forgotten=1)  # type: ignore[call-arg]
    assert list(Parts.model_fields) == [*SIX, "preference"]


def test_the_item_prescore_does_not_use_the_preference_weight():
    candidate = item(1, formality=3, warmth=2)

    with_it = item_prescore(candidate, make_ctx(formality=(3, 3), weights=Weights()))
    without_it = item_prescore(candidate, make_ctx(formality=(3, 3), weights=Weights(preference=0)))

    assert with_it == pytest.approx(without_it)


def _seed_wardrobe() -> list[Item]:
    return [Item(id=n, **raw) for n, raw in enumerate(SEED_ITEMS, start=1)]


def _ranked(weights: Weights):
    history = History(
        last_worn={1: TODAY - timedelta(days=2), 9: TODAY - timedelta(days=10), 14: TODAY - timedelta(days=45)},
        outfit_last_worn={frozenset({1, 9, 14}): TODAY - timedelta(days=5)},
        audience_wears=[],
    )
    ctx = make_ctx(formality=(2, 4), history=history, weights=weights)
    pool, problem = build_pool(_seed_wardrobe(), (2, 4), None, WeatherPrefs())
    assert problem is None
    return rank(pool, ctx, set(), 4, 100_000)  # the cap is far above the number of outfits: it never decides


def test_no_feedback_keeps_the_phase2_order():
    default = _ranked(Weights())
    phase2 = _ranked(Weights(preference=0))

    assert len(default) > 50
    assert {frozenset(c.item_ids) for c in default} == {frozenset(c.item_ids) for c in phase2}
    # With the neutral 0.5 the seven-part score is the six-part score divided by 1.10, plus 0.10 * 0.5 / 1.10.
    # Both lists are sorted best first, so equal values in equal places mean the same order (outfits that tie may swap).
    as_phase2 = [(c.score - 0.05 / 1.10) * 1.10 for c in default]
    assert as_phase2 == pytest.approx([c.score for c in phase2], abs=1e-9)
    assert all(c.parts.preference == 0.5 for c in default)
    assert all(0.0 <= v <= 1.0 for c in default for v in c.parts.model_dump().values())


# --- ratings on outfits (LLD 5.4, 5.6) --------------------------------------------------------------------------------


def _rated_world(engine) -> tuple[int, int, int]:
    """Three outfits (items 1,2 / 3,4 / 5,6): A rated up then down, B rated up, C never rated. Returns their ids."""
    with Session(engine) as s:
        ids = [add_item(s, name=f"Item {n}").id for n in range(1, 7)]
        a, b, c = (add_wear(s, ids[k : k + 2], date(2026, 10, 1 + k)).outfit_id for k in (0, 2, 4))
        for outfit_id, rating in ((a, 1), (b, 1), (a, -1)):
            s.add(Feedback(user_id=OWNER_ID, outfit_id=outfit_id, rating=rating, reason=""))
            s.commit()
    return a, b, c


def test_the_latest_rating_per_outfit_wins_and_an_unrated_outfit_is_left_out(engine):
    a, b, c = _rated_world(engine)

    with Session(engine) as s:
        assert wear.latest_ratings(OWNER_ID, s, [a, b, c]) == {a: -1, b: 1}


def test_latest_ratings_can_be_limited_to_some_outfits_or_none(engine):
    _a, b, _c = _rated_world(engine)

    with Session(engine) as s:
        assert wear.latest_ratings(OWNER_ID, s, [b]) == {b: 1}
        assert wear.latest_ratings(OWNER_ID, s, []) == {}


def test_ratings_by_key_maps_the_item_key_of_each_rated_outfit(engine):
    _rated_world(engine)

    with Session(engine) as s:
        assert wear.ratings_by_key(OWNER_ID, s, ["1,2", "3,4", "5,6", "7,8"]) == {"1,2": -1, "3,4": 1}
        assert wear.ratings_by_key(OWNER_ID, s, []) == {}


def test_the_outfit_list_shows_the_latest_rating_and_null_for_an_unrated_outfit(client):
    a, b, c = _rated_world(client.app.state.ctx.engine)

    by_id = {o["id"]: o for o in client.get("/api/outfits").json()}

    assert [by_id[a]["rating"], by_id[b]["rating"], by_id[c]["rating"]] == [-1, 1, None]


def test_an_item_detail_shows_the_rating_of_the_outfits_it_is_in(client):
    a, _b, _c = _rated_world(client.app.state.ctx.engine)

    outfits = client.get("/api/items/1").json()["outfits"]

    assert [(o["id"], o["rating"]) for o in outfits] == [(a, -1)]


# --- load: what the thumbs taught (AC-20, 23, 24, 49, 53; LLD 5.5)


def rate_set(s: Session, ids: list[int], rating: int) -> int:
    """Rate the saved outfit of these items (saving it first when needed). Returns the outfit id."""
    key = wear.item_key(ids)
    saved = s.exec(select(Outfit).where(Outfit.item_key == key)).first()
    if saved is None:
        saved = Outfit(user_id=OWNER_ID, item_ids=ids, item_key=key, name=f"Outfit {key}")
        s.add(saved)
        s.flush()
    s.add(Feedback(user_id=OWNER_ID, outfit_id=saved.id, rating=rating))
    s.commit()
    return saved.id


def everything(s: Session) -> dict[int, Item]:
    return {i.id: i for i in s.exec(select(Item))}


def make(s: Session, name: str, category: str, **fields) -> Item:
    """An item whose colour, pattern, fabric and subcategory are its own, so two items share only what a test gives."""
    own = name.lower()
    fields = {
        "colors": [{"hex": "#000000", "name": f"c-{own}"}],
        "pattern": f"p-{own}",
        "fabric": f"f-{own}",
        "subcategory": own,
        **fields,
    }
    return add_item(s, name=name, category=category, **fields)


def shirt_outfit_world(s: Session) -> dict[str, list[Item]]:
    """X = navy linen shirt, beige chinos, brown loafers; an outfit sharing some of its tags; one sharing none."""
    x = [
        make(s, "Shirt", "top", colors=[{"hex": "#000080", "name": "navy"}], fabric="linen", pattern="stripe"),
        make(s, "Chinos", "bottom", colors=[{"hex": "#f5f5dc", "name": "beige"}], fabric="twill", pattern="check"),
        make(s, "Loafers", "footwear", colors=[{"hex": "#654321", "name": "brown"}], fabric="leather", pattern="plain"),
    ]
    some = [  # the shirt's colour and fabric, the loafers, and the two categories: part of X's tags, not all
        make(s, "Polo", "top", colors=[{"hex": "#000080", "name": "navy"}], fabric="linen", pattern="dots"),
        make(s, "Jeans", "bottom", colors=[{"hex": "#111111", "name": "grey"}], fabric="denim", pattern="ombre"),
        x[2],
    ]
    none = [  # a dress and a bag: not even a category in common with X
        make(s, "Dress", "dress", colors=[{"hex": "#ff0000", "name": "red"}], fabric="jersey", pattern="floral"),
        make(s, "Clutch", "bag", colors=[{"hex": "#00ff00", "name": "green"}], fabric="silk", pattern="paisley"),
    ]
    return {"x": x, "some": some, "none": none}


@pytest.mark.parametrize("rating", [-1, 1])
def test_sharing_all_some_none_of_a_rated_outfit(engine, rating):
    with Session(engine) as s:
        world = shirt_outfit_world(s)
        rate_set(s, [i.id for i in world["x"]], rating)

        preference = load(OWNER_ID, s, everything(s))

        x, some, none = (preference.part(world[k]) for k in ("x", "some", "none"))
    assert none == 0.5
    if rating < 0:
        assert x < some < none
    else:
        assert x > some > none
    assert preference.part([world["x"][0]]) != 0.5  # one shared item is enough to move the part


def test_latest_rating_counts_only_the_down_after_up_for_that_outfit(engine):
    with Session(engine) as s:
        world = shirt_outfit_world(s)
        ids = [i.id for i in world["x"]]
        rate_set(s, ids, 1)
        rate_set(s, ids, -1)

        preference = load(OWNER_ID, s, everything(s))

    assert preference.net["navy"] == -1
    assert preference.part(world["x"]) == pytest.approx(0.5 + 0.5 * math.tanh(-len(item_tags_of(world["x"])) / 4))


def item_tags_of(items) -> frozenset[str]:
    return frozenset().union(*(item_tags(i) for i in items))


def test_a_down_then_up_on_the_same_outfit_ends_up(engine):
    with Session(engine) as s:
        world = shirt_outfit_world(s)
        ids = [i.id for i in world["x"]]
        rate_set(s, ids, -1)
        rate_set(s, ids, 1)

        assert load(OWNER_ID, s, everything(s)).part(world["x"]) > 0.5


def test_each_rated_outfit_adds_a_tag_once_whatever_the_number_of_its_items_that_carry_it(engine):
    with Session(engine) as s:
        a = make(s, "Shirt", "top", colors=[{"hex": "#000080", "name": "navy"}])
        b = make(s, "Chinos", "bottom", colors=[{"hex": "#000080", "name": "navy"}])
        rate_set(s, [a.id, b.id], -1)

        assert load(OWNER_ID, s, everything(s)).net["navy"] == -1


def test_two_rated_outfits_add_up_and_opposite_thumbs_cancel(engine):
    with Session(engine) as s:
        navy = {"colors": [{"hex": "#000080", "name": "navy"}]}
        a, b, c, d = (make(s, f"Shirt{n}", "top", fabric=f"f{n}", **navy) for n in range(4))
        rate_set(s, [a.id], -1)
        rate_set(s, [b.id], -1)
        rate_set(s, [c.id], 1)
        rate_set(s, [d.id], 1)
        rate_set(s, [d.id, a.id], 1)  # another outfit, so a is in two: its own -1 and this +1

        net = load(OWNER_ID, s, everything(s)).net

    assert net["navy"] == -1 - 1 + 1 + 1 + 1
    assert net["f0"] == -1 + 1


def test_more_down_ratings_never_raise_and_stay_in_unit_range(engine):
    with Session(engine) as s:
        probe = make(s, "Probe", "top", colors=[{"hex": "#000080", "name": "navy"}], fabric="probe")
        parts = [load(OWNER_ID, s, everything(s)).part([probe])]
        for n in range(12):
            shirt = make(s, f"Shirt{n}", "top", colors=[{"hex": "#000080", "name": "navy"}], fabric=f"f{n}")
            rate_set(s, [shirt.id], -1)
            parts.append(load(OWNER_ID, s, everything(s)).part([probe]))

    assert parts[0] == 0.5
    assert parts == sorted(parts, reverse=True) and len(set(parts)) == len(parts)
    assert all(0.0 <= p <= 1.0 for p in parts)


@pytest.mark.parametrize(
    ("rated", "probe"),
    [
        ({"colors": [{"hex": "#000080", "name": "Navy"}]}, {"colors": [{"hex": "#000081", "name": "NAVY"}]}),
        ({"pattern": "Stripe"}, {"pattern": "stripe"}),
        ({"fabric": "Linen"}, {"fabric": "LINEN"}),
        ({"category": "Shorts"}, {"category": "shorts"}),
        ({"subcategory": "Henley"}, {"subcategory": "henley"}),
        ({"tags": ["Silky"]}, {"tags": [" silky "]}),
    ],
    ids=["colour", "pattern", "fabric", "category", "subcategory", "user tag"],
)
def test_shared_attribute_tags_any_case_and_user_tag_lower_preference(engine, rated, probe):
    with Session(engine) as s:
        disliked = make(s, "Disliked", **{"category": "bottom", **rated})
        twin = make(s, "Twin", **{"category": "accessory", **probe})
        stranger = make(s, "Stranger", "bag")
        rate_set(s, [disliked.id], -1)

        preference = load(OWNER_ID, s, everything(s))

        assert preference.part([twin]) < 0.5
        assert preference.part([stranger]) == 0.5


def test_current_tags_count_and_deleted_items_are_skipped(engine):
    with Session(engine) as s:
        a = make(s, "Shirt", "top", tags=["only-a"])
        b = make(s, "Chinos", "bottom", tags=["only-b"])
        c = make(s, "Loafers", "footwear", tags=["old-c"])
        rate_set(s, [a.id, b.id, c.id], -1)
        probes = {
            t: make(s, f"Probe {t}", "accessory", fabric=t, tags=[t]) for t in ("only-a", "only-b", "old-c", "new-c")
        }
        s.delete(s.get(Item, a.id))
        c = s.get(Item, c.id)
        c.tags = ["new-c"]  # edited after the rating: the new tag is the one that counts
        s.add(c)
        s.commit()

        preference = load(OWNER_ID, s, everything(s))

        parts = {t: preference.part([p]) for t, p in probes.items()}
    assert parts["only-a"] == 0.5  # A is gone: nothing of it is counted
    assert parts["only-b"] < 0.5
    assert parts["new-c"] < 0.5 and parts["old-c"] == 0.5


def test_an_outfit_whose_items_are_all_gone_teaches_nothing(engine):
    with Session(engine) as s:
        a = make(s, "Shirt", "top", tags=["only-a"])
        rate_set(s, [a.id], -1)
        s.delete(a)
        s.commit()

        assert load(OWNER_ID, s, everything(s)).net == {}


def test_an_item_missing_from_the_map_is_skipped_even_if_it_still_exists(engine):
    with Session(engine) as s:
        a = make(s, "Shirt", "top", tags=["only-a"])
        b = make(s, "Chinos", "bottom", tags=["only-b"])
        rate_set(s, [a.id, b.id], -1)

        net = load(OWNER_ID, s, {b.id: b}).net

    assert "only-b" in net and "only-a" not in net


def test_load_reads_the_database_a_fixed_number_of_times_however_many_ratings_exist(engine):
    statements: list[str] = []
    with Session(engine) as s:
        items = [make(s, f"Shirt{n}", "top", fabric=f"f{n}") for n in range(30)]
        for it in items:
            rate_set(s, [it.id], -1)
            rate_set(s, [it.id], 1)
        by_id = everything(s)
        event.listen(engine, "before_cursor_execute", lambda *a: statements.append(a[2]))

        load(OWNER_ID, s, by_id)

    assert len(statements) <= 3


# --- the learned part in the ranking (AC-26a, AC-26b) -----------------------------------------------------------------


def two_tops_world(s: Session) -> tuple[Item, Item]:
    """Two tops that are the same except for the user tag, three bottoms, two pairs of shoes; the silky top is liked."""
    plain = {"colors": [{"hex": "#ffffff", "name": "white"}], "pattern": "solid", "fabric": "cotton", "formality": 3}
    silky = add_item(s, name="Silky top", category="top", subcategory="shirt", tags=["silky"], **plain)
    matte = add_item(s, name="Matte top", category="top", subcategory="shirt", tags=["matte"], **plain)
    bottoms = [add_item(s, name=f"Bottom {n}", category="bottom", **{**plain, "fabric": f"b{n}"}) for n in range(3)]
    shoes = [add_item(s, name=f"Shoes {n}", category="footwear", **{**plain, "fabric": f"s{n}"}) for n in range(2)]
    rate_set(s, [silky.id, bottoms[0].id, shoes[0].id], 1)
    return silky, matte


def ranked_with(s: Session, weights: Weights):
    wardrobe = list(s.exec(select(Item)))
    ctx = make_ctx(formality=(2, 4), weights=weights, preference=load(OWNER_ID, s, {i.id: i for i in wardrobe}))
    pool, problem = build_pool(wardrobe, (2, 4), None, WeatherPrefs())
    assert problem is None
    return rank(pool, ctx, set(), 4, 1000)


def test_outfits_with_the_liked_top_rank_above(engine):
    with Session(engine) as s:
        silky, matte = two_tops_world(s)
        got = ranked_with(s, Weights())

    score_of = {frozenset(c.item_ids): c.score for c in got}
    twins = [(ids, ids - {silky.id} | {matte.id}) for ids in score_of if silky.id in ids]
    assert len(twins) == 6
    assert all(score_of[liked] > score_of[other] for liked, other in twins)
    assert got[0].item_ids[0] == silky.id


def test_zero_preference_weight_ties(engine):
    with Session(engine) as s:
        silky, matte = two_tops_world(s)
        got = ranked_with(s, Weights(preference=0))

    by_ids = {frozenset(c.item_ids): c for c in got}
    twins = [(ids, ids - {silky.id} | {matte.id}) for ids in by_ids if silky.id in ids]
    assert twins and all(by_ids[liked].score == by_ids[other].score for liked, other in twins)
    # the part itself is still learned and shown; only its weight is zero
    assert all(by_ids[liked].parts.preference > by_ids[other].parts.preference for liked, other in twins)
