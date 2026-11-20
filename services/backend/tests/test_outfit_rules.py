import pytest

from kyapehnu.core.config import WeatherPrefs
from kyapehnu.wardrobe.outfits.candidates import NO_BLOUSE, build_pool, keeps_pair, rank
from kyapehnu.wardrobe.outfits.schemas import Problem
from kyapehnu.wardrobe.outfits.slots import SLOT_ORDER, Slot, needs_blouse, slot_of
from tests.outfit_fixtures import constraints, item, make_ctx

PREFS = WeatherPrefs()


@pytest.mark.parametrize(
    ("category", "slot"),
    [
        ("top", Slot.top),
        ("bottom", Slot.bottom),
        ("dress", Slot.full),
        ("outerwear", Slot.outerwear),
        ("footwear", Slot.footwear),
        ("accessory", Slot.accessory),
        ("bag", Slot.accessory),
    ],
)
def test_category_maps_to_its_slot(category, slot):
    assert slot_of(item(1, category)) is slot


@pytest.mark.parametrize(
    ("subcategory", "slot"),
    [
        ("kurta", Slot.top),
        ("Sherwani", Slot.top),
        ("BLOUSE", Slot.blouse),
        ("saree blouse", Slot.blouse),
        ("choli", Slot.blouse),
        ("kurti", Slot.top),
        ("kameez", Slot.top),
        ("leggings", Slot.bottom),
        ("sari", Slot.full),
        ("ghagra", Slot.full),
        ("churidar", Slot.bottom),
        ("pyjama", Slot.bottom),
        ("Pyjamas", Slot.bottom),
        ("pajama", Slot.bottom),
        ("salwar", Slot.bottom),
        ("Dhoti", Slot.bottom),
        ("patiala", Slot.bottom),
        ("sharara", Slot.bottom),
        ("palazzo", Slot.bottom),
        ("SAREE", Slot.full),
        ("lehenga", Slot.full),
        ("anarkali", Slot.full),
        ("gown", Slot.full),
        ("Nehru Jacket", Slot.outerwear),
        ("nehru  jacket", Slot.outerwear),
        ("bandhgala", Slot.outerwear),
        ("achkan", Slot.outerwear),
        ("shrug", Slot.outerwear),
        ("dupatta", Slot.accessory),
        ("Stole", Slot.accessory),
    ],
)
def test_ethnic_subcategory_maps_to_its_slot_in_any_case(subcategory, slot):
    assert slot_of(item(1, "ethnic", subcategory=subcategory)) is slot


@pytest.mark.parametrize(
    ("subcategory", "slot"),
    [("Silk Kurta", Slot.top), ("cotton salwar", Slot.bottom), ("embroidered saree set", Slot.full)],
)
def test_ethnic_word_matches_inside_a_longer_name(subcategory, slot):
    assert slot_of(item(1, "ethnic", subcategory=subcategory)) is slot


@pytest.mark.parametrize("subcategory", ["kurtapalooza", "phulkari", "", "pyjamaparty"])
def test_ethnic_without_a_whole_word_match_falls_back_to_top(subcategory):
    assert slot_of(item(1, "ethnic", subcategory=subcategory)) is Slot.top


def test_the_first_matching_slot_wins_for_ethnic():
    assert slot_of(item(1, "ethnic", subcategory="kurta pyjama")) is Slot.top


def test_the_slot_order_is_the_template_order():
    assert SLOT_ORDER == (
        Slot.top,
        Slot.bottom,
        Slot.blouse,
        Slot.full,
        Slot.outerwear,
        Slot.footwear,
        Slot.accessory,
        Slot.watch,
        Slot.cap,
        Slot.sunglasses,
        Slot.earrings,
        Slot.necklace,
        Slot.ring,
        Slot.bangles,
    )


def test_a_western_blouse_is_an_ordinary_top():
    assert slot_of(item(1, "top", subcategory="blouse", name="Silk blouse")) is Slot.top


@pytest.mark.parametrize(
    ("subcategory", "name", "slot"),
    [
        ("earrings", "Gold jhumkas", Slot.earrings),
        ("", "Silver hoops", Slot.earrings),
        ("necklace", "Pearl choker", Slot.necklace),
        ("", "Mangalsutra", Slot.necklace),
        ("ring", "Ruby ring", Slot.ring),
        ("bangles", "Glass bangles", Slot.bangles),
        ("bracelet", "Gold kada", Slot.bangles),
        ("watch", "Bracelet watch", Slot.watch),
        ("maang tikka", "Kundan tikka", Slot.accessory),
    ],
)
def test_jewellery_rides_along_by_its_words(subcategory, name, slot):
    assert slot_of(item(1, "accessory", subcategory=subcategory, name=name)) is slot


def test_jewellery_words_only_count_for_accessories():
    assert slot_of(item(1, "top", subcategory="top", name="Ring-neck top")) is Slot.top


@pytest.mark.parametrize(
    ("words", "needed"),
    [("saree Green silk saree", True), ("lehenga Red lehenga", True), ("kurti Coral kurti", False), ("sarees", True)],
)
def test_needs_blouse(words, needed):
    assert needs_blouse(words) is needed


def basic_wardrobe():
    return [
        item(1, "top"),
        item(2, "bottom"),
        item(3, "footwear"),
        item(4, "outerwear"),
        item(5, "accessory"),
        item(6, "dress"),
    ]


def pool_of(wardrobe, formality=(1, 5), cons=None):
    pool, problem = build_pool(wardrobe, formality, cons, PREFS)
    assert problem is None
    return pool


@pytest.mark.parametrize("status", ["laundry", "tailor", "packed", "retired"])
def test_items_that_are_not_available_never_enter_the_pool(status):
    pool = pool_of(basic_wardrobe() + [item(9, "top", status=status)])

    assert [i.id for i in pool.by_slot[Slot.top]] == [1]


@pytest.mark.parametrize(("formality", "kept"), [(2, False), (3, True), (4, True), (5, False)])
def test_formality_range_is_inclusive_at_both_ends(formality, kept):
    pool = pool_of(basic_wardrobe() + [item(9, "top", formality=formality)], formality=(3, 4))

    assert (9 in [i.id for i in pool.by_slot[Slot.top]]) is kept


@pytest.mark.parametrize(("warmth", "kept"), [(1, False), (2, True), (4, True), (5, False)])
def test_warmth_is_filtered_inside_the_band_inclusive(warmth, kept):
    cons = constraints(warmth_min=2, warmth_max=4)

    pool = pool_of([item(1, "top", warmth=3), item(2, "bottom", warmth=3), item(9, "top", warmth=warmth)], cons=cons)

    assert (9 in [i.id for i in pool.by_slot[Slot.top]]) is kept


def test_warmth_filters_footwear_when_another_pair_fits():
    cons = constraints(warmth_min=1, warmth_max=2)
    wardrobe = [
        item(1, "top", warmth=2),
        item(2, "bottom", warmth=1),
        item(3, "footwear", warmth=4),
        item(4, "footwear", warmth=1),
    ]

    pool = pool_of(wardrobe, cons=cons)

    assert [i.id for i in pool.by_slot[Slot.footwear]] == [4]


def test_warmth_does_not_filter_without_weather():
    pool = pool_of([item(1, "top", warmth=1), item(2, "top", warmth=5), item(3, "bottom")], cons=None)

    assert [i.id for i in pool.by_slot[Slot.top]] == [1, 2]


def test_items_are_grouped_by_slot():
    pool = pool_of(basic_wardrobe())

    assert {slot: [i.id for i in items] for slot, items in pool.by_slot.items()} == {
        Slot.top: [1],
        Slot.bottom: [2],
        Slot.blouse: [],
        Slot.full: [6],
        Slot.outerwear: [4],
        Slot.footwear: [3],
        Slot.accessory: [5],
        Slot.watch: [],
        Slot.cap: [],
        Slot.sunglasses: [],
        Slot.earrings: [],
        Slot.necklace: [],
        Slot.ring: [],
        Slot.bangles: [],
    }


def test_layer_none_empties_outerwear():
    assert pool_of(basic_wardrobe(), cons=constraints(layer="none")).by_slot[Slot.outerwear] == []


@pytest.mark.parametrize("layer", ["optional", "wanted"])
def test_other_layers_keep_outerwear(layer):
    assert len(pool_of(basic_wardrobe(), cons=constraints(layer=layer)).by_slot[Slot.outerwear]) == 1


def test_no_footwear_left_adds_the_note():
    pool = pool_of([item(1, "top"), item(2, "bottom")])

    assert pool.notes == ("no_footwear",)


def test_footwear_present_has_no_note():
    assert pool_of(basic_wardrobe()).notes == ()


@pytest.mark.parametrize(
    ("formality", "cons"),
    [((3, 5), None), ((1, 5), constraints(warmth_min=3, warmth_max=5))],
    ids=["too casual for the dress level", "too light for the weather"],
)
def test_footwear_outside_the_range_stands_in_when_none_fits(formality, cons):
    """Shoes that miss the dress level or the weather still come in when no pair fits: the nearest pair beats none.
    A pair that is not available stays out."""
    wardrobe = [
        item(1, "top", formality=4, warmth=4),
        item(2, "bottom", formality=4, warmth=4),
        item(3, "footwear", formality=2, warmth=1),
        item(4, "footwear", formality=2, warmth=1, status="laundry"),
    ]

    pool = pool_of(wardrobe, formality=formality, cons=cons)

    assert [i.id for i in pool.by_slot[Slot.footwear]] == [3] and pool.notes == ()


def test_rain_keeps_only_rain_safe_footwear_when_some_exist():
    wardrobe = [
        item(1, "top"),
        item(2, "bottom"),
        item(3, "footwear", fabric="suede"),
        item(4, "footwear", fabric="leather"),
    ]

    pool = pool_of(wardrobe, cons=constraints(rain_likely=True))

    assert [i.id for i in pool.by_slot[Slot.footwear]] == [4]
    assert pool.notes == ()


def test_rain_with_only_unsafe_footwear_keeps_all_and_notes_it():
    wardrobe = [
        item(1, "top"),
        item(2, "bottom"),
        item(3, "footwear", fabric="suede"),
        item(4, "footwear", name="canvas shoe"),
    ]

    pool = pool_of(wardrobe, cons=constraints(rain_likely=True))

    assert [i.id for i in pool.by_slot[Slot.footwear]] == [3, 4]
    assert pool.notes == ("footwear_not_rain_safe",)


def test_dry_weather_keeps_suede_footwear():
    wardrobe = [item(1, "top"), item(2, "bottom"), item(3, "footwear", fabric="suede")]

    assert [i.id for i in pool_of(wardrobe, cons=constraints()).by_slot[Slot.footwear]] == [3]


def problem_of(wardrobe, formality=(1, 5), cons=None) -> Problem | None:
    return build_pool(wardrobe, formality, cons, PREFS)[1]


def test_no_bottom_and_no_full_item_is_a_missing_bottom():
    problem = problem_of([item(1, "top"), item(3, "footwear")])

    assert problem == Problem(
        slot=Slot.bottom, cause="missing", message="Add at least one bottom to get outfit suggestions"
    )


def test_nothing_at_all_names_the_top_first():
    problem = problem_of([item(3, "footwear")])

    assert problem == Problem(slot=Slot.top, cause="missing", message="Add at least one top to get outfit suggestions")


def test_bottoms_that_all_fail_the_formality_range_are_filtered():
    problem = problem_of([item(1, "top", formality=3), item(2, "bottom", formality=1)], formality=(3, 4))

    assert problem == Problem(
        slot=Slot.bottom,
        cause="filtered",
        message="No bottom fits this event: every bottom was filtered out by status, formality or weather",
    )


def test_a_bottom_in_the_laundry_is_filtered_not_missing():
    problem = problem_of([item(1, "top"), item(2, "bottom", status="laundry")])

    assert problem is not None and problem.cause == "filtered"


def test_a_full_item_alone_is_enough():
    assert problem_of([item(6, "dress")]) is None


def test_a_full_item_covers_for_a_missing_top():
    assert problem_of([item(2, "bottom"), item(6, "dress")]) is None


def test_weather_filtering_everything_is_a_filtered_problem():
    cons = constraints(warmth_min=1, warmth_max=2)

    problem = problem_of([item(1, "top", warmth=5), item(2, "bottom", warmth=1)], cons=cons)

    assert problem is not None and (problem.slot, problem.cause) == (Slot.top, "filtered")


def ids_of(candidate):
    return {i.id for _, i in candidate.items}


def rank_all(wardrobe, ctx=None, excluded=frozenset(), per_slot=8, max_candidates=300):
    ctx = ctx or make_ctx()
    pool = pool_of(wardrobe, cons=ctx.cons)  # the pool takes everything: only the ranking sees ctx.formality
    return rank(pool, ctx, set(excluded), per_slot, max_candidates)


def test_every_candidate_is_top_bottom_footwear_or_full_footwear():
    ranked = rank_all(basic_wardrobe())

    shapes = {frozenset(slot for slot, _ in c.items) - {Slot.outerwear, Slot.accessory} for c in ranked}
    assert shapes == {frozenset({Slot.top, Slot.bottom, Slot.footwear}), frozenset({Slot.full, Slot.footwear})}


def test_a_candidate_has_at_most_one_outerwear_and_one_accessory():
    wardrobe = basic_wardrobe() + [item(7, "outerwear"), item(8, "accessory"), item(9, "bag")]

    for candidate in rank_all(wardrobe):
        slots = [slot for slot, _ in candidate.items]
        assert slots.count(Slot.outerwear) <= 1 and slots.count(Slot.accessory) <= 1


def test_candidate_items_are_in_slot_order():
    for candidate in rank_all(basic_wardrobe()):
        order = [SLOT_ORDER.index(slot) for slot, _ in candidate.items]
        assert order == sorted(order)
        assert candidate.item_ids == [i.id for _, i in candidate.items]
        assert candidate.ids == frozenset(candidate.item_ids)


def test_candidates_without_footwear_carry_the_note():
    ranked = rank_all([item(1, "top"), item(2, "bottom")])

    assert ranked and all(Slot.footwear not in {s for s, _ in c.items} and c.notes == ("no_footwear",) for c in ranked)


def test_unsafe_footwear_note_is_on_every_candidate():
    wardrobe = [item(1, "top"), item(2, "bottom"), item(3, "footwear", fabric="suede")]

    ranked = rank_all(wardrobe, make_ctx(cons=constraints(rain_likely=True)))

    assert ranked and all(c.notes == ("footwear_not_rain_safe",) for c in ranked)


def has_outerwear(candidate):
    return Slot.outerwear in {s for s, _ in candidate.items}


def test_a_wanted_layer_puts_outerwear_in_every_candidate():
    ranked = rank_all(basic_wardrobe(), make_ctx(cons=constraints(layer="wanted")))

    assert ranked and all(has_outerwear(c) for c in ranked)


def test_a_wanted_layer_without_any_outerwear_still_builds_outfits():
    ranked = rank_all([item(1), item(2, "bottom"), item(3, "footwear")], make_ctx(cons=constraints(layer="wanted")))

    assert ranked and not any(has_outerwear(c) for c in ranked)


def test_no_layer_means_no_outerwear():
    ranked = rank_all(basic_wardrobe(), make_ctx(cons=constraints(layer="none")))

    assert ranked and not any(has_outerwear(c) for c in ranked)


@pytest.mark.parametrize("cons", [constraints(layer="optional"), None])
def test_an_optional_layer_or_no_weather_offers_both_forms(cons):
    ranked = rank_all(basic_wardrobe(), make_ctx(cons=cons))

    assert {has_outerwear(c) for c in ranked} == {True, False}


def test_only_the_per_slot_best_items_by_prescore_are_combined():
    # range (3, 3): formality 3 is the best, 1 and 5 the worst; ties go to the lower id (AC-16)
    tops = [item(n, formality=3 if 11 <= n <= 14 else 1) for n in range(1, 21)]
    wardrobe = tops + [item(30, "bottom", formality=3), item(31, "footwear", formality=3)]

    ranked = rank_all(wardrobe, make_ctx(formality=(3, 3)), per_slot=8)

    used = {i.id for c in ranked for s, i in c.items if s is Slot.top}
    assert used == {11, 12, 13, 14, 1, 2, 3, 4}


def big_wardrobe():
    return [
        item(n, category, formality=3)
        for category, base in (("top", 0), ("bottom", 100), ("footwear", 200))
        for n in range(base + 1, base + 9)
    ]


@pytest.mark.parametrize(("cap", "expected"), [(300, 300), (50, 50), (600, 512)])
def test_expansion_is_capped_at_max_candidates(cap, expected):
    ranked = rank_all(big_wardrobe(), max_candidates=cap)

    assert len(ranked) == expected  # 8 x 8 x 8 = 512 combinations


def test_the_cap_keeps_the_candidates_with_the_best_item_prescores():
    wardrobe = [
        item(1, formality=3),
        item(2, formality=2),
        item(3, formality=1),
        item(4, "bottom", formality=3),
        item(5, "footwear", formality=3),
    ]

    ranked = rank_all(wardrobe, make_ctx(formality=(3, 3)), max_candidates=2)

    assert sorted(ids_of(c) for c in ranked) == [{1, 4, 5}, {2, 4, 5}]


def test_cap_ties_are_broken_by_expansion_order():
    wardrobe = [item(n, formality=3) for n in (1, 2, 3)] + [
        item(4, "bottom", formality=3),
        item(5, "footwear", formality=3),
    ]

    ranked = rank_all(wardrobe, make_ctx(formality=(3, 3)), max_candidates=1)

    assert ids_of(ranked[0]) == {1, 4, 5}


def test_ranked_candidates_are_sorted_by_score_descending():
    ranked = rank_all(big_wardrobe(), max_candidates=40)

    scores = [c.score for c in ranked]
    assert scores == sorted(scores, reverse=True)


def test_an_excluded_set_is_never_returned():
    ranked = rank_all(basic_wardrobe(), excluded=[frozenset({1, 2, 3})])

    assert frozenset({1, 2, 3}) not in {c.ids for c in ranked}
    assert frozenset({1, 2, 3, 4}) in {c.ids for c in ranked}  # a superset is a different outfit


def test_exclusion_is_applied_before_the_cap():
    wardrobe = [item(1), item(2), item(3, "bottom"), item(4, "footwear")]

    ranked = rank_all(wardrobe, None, excluded=[frozenset({1, 3, 4})], max_candidates=1)

    assert [c.ids for c in ranked] == [frozenset({2, 3, 4})]


def test_the_remaining_ids_of_an_incomplete_outfit_are_not_suggested_as_is():
    # outfit {1, 2, 3, B} lost B: its remaining ids are excluded (AC-4)
    wardrobe = [item(1), item(2, "bottom"), item(3, "footwear")]

    assert rank_all(wardrobe, excluded=[frozenset({1, 2, 3})]) == []


def test_candidates_carry_a_score_and_parts():
    ranked = rank_all(basic_wardrobe())

    assert all(0 <= c.score <= 1 and 0 <= c.parts.colour <= 1 for c in ranked)


@pytest.mark.parametrize(
    ("subcategory", "name", "slot"),
    [
        ("watch", "Steel watch", Slot.watch),
        ("", "Casio smartwatch", Slot.watch),
        ("cap", "Navy cap", Slot.cap),
        ("hat", "Straw hat", Slot.cap),
        ("Beanie", "Grey one", Slot.cap),
        ("sunglasses", "Aviators", Slot.sunglasses),
        ("", "Black shades", Slot.sunglasses),
        ("belt", "Brown belt", Slot.accessory),
        ("scarf", "Wool scarf", Slot.accessory),
    ],
)
def test_an_accessory_is_a_watch_a_cap_or_sunglasses_by_its_words(subcategory, name, slot):
    assert slot_of(item(1, "accessory", subcategory=subcategory, name=name)) is slot


def test_a_bag_is_never_an_extra_whatever_its_name():
    assert slot_of(item(1, "bag", subcategory="cap", name="Cap bag")) is Slot.accessory


def extras_wardrobe():
    return basic_wardrobe() + [
        item(7, "accessory", subcategory="watch", name="Watch"),
        item(8, "accessory", subcategory="cap", name="Cap"),
        item(9, "accessory", subcategory="sunglasses", name="Shades"),
    ]


def test_a_watch_a_cap_and_sunglasses_ride_along_with_every_candidate():
    ranked = rank_all(extras_wardrobe())

    assert ranked
    for candidate in ranked:
        slots = [slot for slot, _ in candidate.items]
        assert slots.count(Slot.watch) == slots.count(Slot.cap) == slots.count(Slot.sunglasses) == 1
        assert slots.count(Slot.accessory) <= 1


def test_extras_are_taken_in_turn_so_the_candidates_differ():
    wardrobe = extras_wardrobe() + [item(10, "accessory", subcategory="watch", name="Other watch")]

    watches = {next(i.id for slot, i in c.items if slot is Slot.watch) for c in rank_all(wardrobe)}

    assert watches == {7, 10}


def test_extras_still_follow_the_formality_range():
    wardrobe = extras_wardrobe() + [item(10, "accessory", subcategory="cap", name="Snapback", formality=1)]

    pool = pool_of(wardrobe, formality=(3, 5))

    assert [i.id for i in pool.by_slot[Slot.cap]] == [8]


def test_a_watch_and_sunglasses_ignore_the_warmth_band_but_a_cap_does_not():
    cons = constraints(warmth_min=4, warmth_max=5)
    wardrobe = [item(1, "top", warmth=4), item(2, "bottom", warmth=4), item(3, "footwear", warmth=4)] + [
        item(7, "accessory", subcategory="watch", name="Watch", warmth=3),
        item(8, "accessory", subcategory="cap", name="Cap", warmth=3),
        item(9, "accessory", subcategory="sunglasses", name="Shades", warmth=3),
    ]

    pool = pool_of(wardrobe, cons=cons)

    assert (pool.by_slot[Slot.watch], pool.by_slot[Slot.cap], pool.by_slot[Slot.sunglasses]) == (
        [wardrobe[3]],
        [],
        [wardrobe[5]],
    )


def saree_wardrobe():
    return [
        item(1, "ethnic", subcategory="saree", name="Silk saree"),
        item(2, "ethnic", subcategory="blouse", name="Saree blouse"),
        item(3, "footwear", subcategory="heels"),
    ]


def test_a_saree_is_always_worn_with_a_blouse():
    ranked = rank_all(saree_wardrobe())

    assert ranked
    for candidate in ranked:
        assert {slot for slot, _ in candidate.items} >= {Slot.blouse, Slot.full}


def test_a_blouse_never_goes_with_jeans():
    ranked = rank_all(saree_wardrobe() + [item(4, "bottom", subcategory="jeans")])

    assert ranked
    assert all(Slot.bottom not in {slot for slot, _ in c.items} for c in ranked)


def test_a_saree_without_a_blouse_is_left_out_and_noted():
    wardrobe = [item(1, "ethnic", subcategory="saree"), item(5, "top"), item(4, "bottom"), item(3, "footwear")]

    pool, problem = build_pool(wardrobe, (1, 5), None, PREFS)

    assert pool.by_slot[Slot.full] == []
    assert NO_BLOUSE in pool.notes
    assert problem is None


def test_only_a_saree_and_no_blouse_names_the_blouse():
    _, problem = build_pool([item(1, "ethnic", subcategory="saree")], (1, 5), None, PREFS)

    assert problem is not None
    assert (problem.slot, problem.cause) == (Slot.blouse, "missing")
    assert "blouse" in problem.message


def test_a_lehenga_pairs_with_a_choli_and_a_dress_stands_alone():
    wardrobe = [
        item(1, "ethnic", subcategory="lehenga"),
        item(2, "ethnic", subcategory="choli"),
        item(6, "dress"),
        item(3, "footwear"),
    ]

    bases = {tuple(s for s, _ in c.items if s in (Slot.blouse, Slot.full)) for c in rank_all(wardrobe)}

    assert bases == {(Slot.blouse, Slot.full), (Slot.full,)}


def test_jewellery_rides_along_and_ignores_the_warmth_band():
    ctx = make_ctx(cons=constraints(warmth_min=4, warmth_max=5))
    wardrobe = [item(1, "top", warmth=4), item(2, "bottom", warmth=4), item(3, "footwear", warmth=4)] + [
        item(7, "accessory", subcategory="earrings", name="Jhumkas", warmth=1),
        item(8, "accessory", subcategory="necklace", name="Choker", warmth=1),
    ]

    ranked = rank_all(wardrobe, ctx=ctx)

    assert ranked
    assert all({slot for slot, _ in c.items} >= {Slot.earrings, Slot.necklace} for c in ranked)


@pytest.mark.parametrize(
    ("spare", "slot", "held", "keeps"),
    [
        (item(1, "dress"), Slot.full, {Slot.blouse, Slot.full, Slot.footwear}, False),
        (item(1, "ethnic", subcategory="saree"), Slot.full, {Slot.blouse, Slot.full, Slot.footwear}, True),
        (item(1, "ethnic", subcategory="saree"), Slot.full, {Slot.full, Slot.footwear}, False),
        (item(1, "dress"), Slot.full, {Slot.full, Slot.footwear}, True),
        (item(1, "top"), Slot.top, {Slot.top, Slot.bottom}, True),
    ],
)
def test_a_full_spare_keeps_the_blouse_pairing(spare, slot, held, keeps):
    assert keeps_pair(spare, slot, frozenset(held)) is keeps


def test_a_blouse_follows_its_saree_not_the_formality_range_or_the_warmth_band():
    wardrobe = [
        item(1, "ethnic", subcategory="saree", formality=5, warmth=2),
        item(2, "ethnic", subcategory="blouse", formality=3, warmth=5),
    ]

    pool = pool_of(wardrobe, formality=(4, 5), cons=constraints(warmth_min=1, warmth_max=2))

    assert [i.id for i in pool.by_slot[Slot.blouse]] == [2]
    assert [i.id for i in pool.by_slot[Slot.full]] == [1]
