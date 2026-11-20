from datetime import timedelta

import pytest

from kyapehnu.core.config import RepetitionPrefs, Weights
from kyapehnu.wardrobe.outfits.repetition import (
    History,
    audience,
    exact_outfit,
    forgotten_part,
    item_recency,
    repetition_part,
    standout,
)
from kyapehnu.wardrobe.outfits.schemas import Parts
from kyapehnu.wardrobe.outfits.scoring import (
    formality_fit,
    item_prescore,
    item_weather_fit,
    pattern_score,
    score,
    weather_fit,
    weighted_total,
)
from kyapehnu.wardrobe.outfits.slots import Slot
from tests.outfit_fixtures import TODAY, constraints, item, make_ctx


def days_ago(n: int):
    return TODAY - timedelta(days=n)


def slotted(*items):
    return [(slot, i) for slot, i in items]


@pytest.mark.parametrize(("non_solid", "expected"), [(0, 1.0), (1, 1.0), (2, 0.5), (3, 0.25), (4, 0.125)])
def test_pattern_score_halves_for_each_extra_pattern(non_solid, expected):
    items = [item(n, pattern="stripe") for n in range(non_solid)] + [item(10 + n, pattern="solid") for n in range(2)]

    assert pattern_score(items) == expected


def test_every_non_solid_kind_counts_as_a_pattern():
    kinds = ["stripe", "check", "print", "floral", "embroidered", "other"]

    assert pattern_score([item(n, pattern=kind) for n, kind in enumerate(kinds)]) == 0.5**5


def test_formality_scores_one_when_even_and_at_the_middle():
    assert formality_fit([3, 3, 3], (3, 3)) == 1.0


def test_the_middle_of_a_two_level_range_is_the_half_step_between_them():
    # average 3.5 sits on the middle of (3, 4): closeness 1; spread 0.5: evenness 0.75
    assert formality_fit([3, 4], (3, 4)) == pytest.approx(0.5 * 1 + 0.5 * 0.75)


def test_an_even_set_beats_a_spread_set_with_the_same_average():
    assert formality_fit([3, 4, 3, 4], (3, 4)) > formality_fit([1, 5, 3, 4], (3, 4))


def test_a_spread_with_the_right_average_keeps_only_the_closeness_half():
    assert formality_fit([1, 5], (3, 3)) == pytest.approx(0.5)  # closeness 1, evenness 0


def test_an_average_far_from_the_middle_scores_lower_than_one_at_it():
    assert formality_fit([4, 4], (3, 3)) == pytest.approx(0.75)  # closeness 0.5, evenness 1
    assert formality_fit([5, 5], (3, 3)) == pytest.approx(0.5)  # closeness 0, evenness 1
    assert formality_fit([4, 4], (3, 3)) < formality_fit([3, 3], (3, 3))


def test_item_weather_fit_is_half_without_weather():
    assert item_weather_fit(item(1), None) == 0.5
    assert weather_fit([item(1), item(2)], None) == 0.5


@pytest.mark.parametrize(("warmth", "expected"), [(3, 1.0), (1, 0.5), (5, 0.5), (4, 0.75)])
def test_item_weather_fit_falls_with_distance_from_the_band_middle(warmth, expected):
    cons = constraints(warmth_min=2, warmth_max=4)  # target 3

    assert item_weather_fit(item(1, warmth=warmth), cons) == expected


def test_item_weather_fit_is_zero_at_the_greatest_possible_distance():
    assert item_weather_fit(item(1, warmth=1), constraints(warmth_min=5, warmth_max=5)) == 0.0


def test_a_marked_down_fabric_halves_the_fit():
    cons = constraints(warmth_min=3, warmth_max=3, marked_down=frozenset({"polyester"}))

    assert item_weather_fit(item(1, warmth=3, fabric="polyester"), cons) == 0.5
    assert item_weather_fit(item(2, warmth=3, fabric="cotton"), cons) == 1.0


def test_polyester_scores_below_cotton_when_hot_and_humid():
    cons = constraints(warmth_min=1, warmth_max=2, marked_down=frozenset({"polyester"}))

    assert weather_fit([item(1, warmth=1, fabric="polyester")], cons) < weather_fit(
        [item(1, warmth=1, fabric="cotton")], cons
    )


def test_weather_fit_is_the_mean_over_items():
    cons = constraints(warmth_min=3, warmth_max=3)

    assert weather_fit([item(1, warmth=3), item(2, warmth=1)], cons) == pytest.approx((1.0 + 0.5) / 2)


def test_forgotten_is_the_share_never_worn_or_not_worn_for_sixty_days():
    items = [item(1), item(2), item(3)]
    last_worn = {2: days_ago(70), 3: days_ago(1)}

    assert forgotten_part(items, last_worn, TODAY, 60) == pytest.approx(2 / 3)


@pytest.mark.parametrize(("days", "forgotten"), [(59, 0.0), (60, 1.0), (61, 1.0)])
def test_forgotten_boundary_is_the_configured_number_of_days(days, forgotten):
    assert forgotten_part([item(1)], {1: days_ago(days)}, TODAY, 60) == forgotten


def test_nothing_forgotten_in_an_empty_outfit():
    assert forgotten_part([], {}, TODAY, 60) == 0.0


@pytest.mark.parametrize(("days", "expected"), [(0, 1.0), (14, 0.5), (28, 0.25), (7, 0.5**0.5)])
def test_item_recency_halves_every_half_life(days, expected):
    assert item_recency(days_ago(days), TODAY, 14) == pytest.approx(expected)


def test_a_never_worn_item_has_no_recency_penalty():
    assert item_recency(None, TODAY, 14) == 0.0


def test_a_wear_dated_in_the_future_counts_as_today():
    assert item_recency(TODAY + timedelta(days=3), TODAY, 14) == 1.0


@pytest.mark.parametrize(
    ("days", "expected"), [(0, 0.8), (15, 0.4), (29, pytest.approx(0.8 / 30)), (30, 0.0), (45, 0.0)]
)
def test_exact_outfit_penalty_fades_over_the_window(days, expected):
    assert exact_outfit(days_ago(days), TODAY, 30) == expected


def test_exact_outfit_is_zero_when_never_worn():
    assert exact_outfit(None, TODAY, 30) == 0.0


def wear(*ids, days=60):
    return frozenset(ids), days_ago(days)


def test_audience_penalty_is_point_nine_for_the_same_outfit():
    assert audience(frozenset({1, 2, 3}), 3, [wear(1, 2, 3)]) == 0.9


def test_audience_penalty_is_half_when_only_the_standout_was_shared():
    assert audience(frozenset({1, 2, 3}), 3, [wear(3, 8, 9)]) == 0.5


def test_audience_is_zero_without_a_shared_outfit_or_standout():
    assert audience(frozenset({1, 2, 3}), 3, [wear(1, 2, 8)]) == 0.0
    assert audience(frozenset({1, 2, 3}), 3, []) == 0.0


def test_audience_takes_the_largest_over_the_wears():
    assert audience(frozenset({1, 2, 3}), 3, [wear(3, 9), wear(1, 2, 3)]) == 0.9
    assert audience(frozenset({1, 2, 3}), 3, [wear(1, 2, 3), wear(3, 9)]) == 0.9


def test_the_outerwear_is_the_standout():
    items = slotted(
        (Slot.top, item(1, pattern="floral")), (Slot.outerwear, item(2)), (Slot.footwear, item(3, pattern="print"))
    )

    assert standout(items).id == 2


def test_without_outerwear_the_boldest_pattern_is_the_standout():
    items = slotted(
        (Slot.top, item(1, pattern="stripe")),
        (Slot.bottom, item(2, pattern="floral")),
        (Slot.footwear, item(3, pattern="print")),
    )

    assert standout(items).id == 2


@pytest.mark.parametrize(
    ("milder", "bolder"),
    [("solid", "stripe"), ("stripe", "check"), ("check", "embroidered"), ("print", "floral")],
)
def test_pattern_boldness_ranks_solid_to_floral(milder, bolder):
    items = slotted((Slot.top, item(1, pattern=milder)), (Slot.bottom, item(2, pattern=bolder)))

    assert standout(items).id == 2


@pytest.mark.parametrize(("first", "second"), [("check", "other"), ("embroidered", "print")])
def test_patterns_of_equal_boldness_tie(first, second):
    items = slotted((Slot.top, item(1, pattern=first)), (Slot.bottom, item(2, pattern=second)))

    assert standout(items).id == 1


def test_a_boldness_tie_goes_to_the_earlier_slot():
    items = slotted(
        (Slot.bottom, item(2, pattern="print")), (Slot.top, item(1, pattern="print")), (Slot.footwear, item(3))
    )

    assert standout(items).id == 1


def history(**fields):
    return History(**{"last_worn": {}, "outfit_last_worn": {}, "audience_wears": [], **fields})


def outfit():
    return slotted((Slot.top, item(1)), (Slot.bottom, item(2)), (Slot.footwear, item(3)))


PREFS = RepetitionPrefs()


def repetition(hist, items=None):
    return repetition_part(items or outfit(), hist, TODAY, PREFS)


def test_repetition_is_one_when_nothing_was_ever_worn():
    assert repetition(history()) == 1.0


def test_the_recency_penalty_is_the_mean_over_the_items():
    hist = history(last_worn={1: days_ago(14), 2: days_ago(14), 3: days_ago(14)})

    assert repetition(hist) == pytest.approx(0.5)
    assert repetition(history(last_worn={1: days_ago(14)})) == pytest.approx(1 - 0.5 / 3)


def test_the_penalties_add_up():
    hist = history(
        last_worn={1: days_ago(28), 2: days_ago(28), 3: days_ago(28)},
        outfit_last_worn={frozenset({1, 2, 3}): days_ago(15)},
    )

    assert repetition(hist) == pytest.approx(1 - (0.25 + 0.4))


def test_penalties_above_one_are_capped_so_repetition_is_zero():
    hist = history(
        last_worn={1: days_ago(0), 2: days_ago(0), 3: days_ago(0)},
        outfit_last_worn={frozenset({1, 2, 3}): days_ago(0)},
        audience_wears=[wear(1, 2, 3)],
    )

    assert repetition(hist) == 0.0


def test_the_audience_penalty_uses_the_standout_piece():
    items = slotted((Slot.top, item(1, pattern="floral")), (Slot.bottom, item(2)), (Slot.footwear, item(3)))

    assert repetition(history(audience_wears=[wear(1, 8)]), items) == pytest.approx(0.5)


def test_a_history_with_only_item_last_worn_dates_gives_the_recency_penalty_alone():
    item_only = history(last_worn={1: days_ago(28), 2: days_ago(28), 3: days_ago(28)})

    assert repetition(item_only) == pytest.approx(1 - 0.25)  # recency alone: no exact or audience entry exists


def test_parts_weighted_with_the_default_weights():
    parts = Parts(colour=1, pattern=0.5, formality=0.8, weather=0.5, repetition=0.4, forgotten=1, preference=0.5)

    # the seven default weights, each divided by their sum 1.10
    expected = (0.25 * 1 + 0.10 * 0.5 + 0.20 * 0.8 + 0.20 * 0.5 + 0.20 * 0.4 + 0.05 * 1 + 0.10 * 0.5) / 1.10
    assert weighted_total(parts, Weights()) == pytest.approx(expected)


def test_changed_weights_are_used():
    parts = Parts(colour=0.9, pattern=0.1, formality=0.1, weather=0.1, repetition=0.1, forgotten=0.1, preference=0.1)
    only_colour = Weights(colour=1, pattern=0, formality=0, weather=0, repetition=0, forgotten=0, preference=0)

    assert weighted_total(parts, only_colour) == pytest.approx(0.9)


def test_score_is_the_weighted_sum_of_parts_each_in_unit_range():
    items = slotted(
        (Slot.top, item(1, pattern="stripe", colors=[{"hex": "#ff0000", "name": "red"}])),
        (Slot.bottom, item(2, colors=[{"hex": "#ffbf00", "name": "amber"}])),
        (Slot.footwear, item(3, pattern="check")),
    )
    ctx = make_ctx(
        formality=(2, 4), cons=constraints(warmth_min=2, warmth_max=4), history=history(last_worn={1: days_ago(3)})
    )

    total, parts = score(items, ctx)

    assert total == pytest.approx(weighted_total(parts, ctx.weights))
    assert all(0 <= value <= 1 for value in parts.model_dump().values())
    assert (parts.colour, parts.pattern) == (0.3, 0.5)


def test_score_parts_use_each_rule():
    items = outfit()
    ctx = make_ctx(formality=(3, 3), cons=None)

    _, parts = score(items, ctx)

    assert parts == Parts(
        colour=1.0, pattern=1.0, formality=1.0, weather=0.5, repetition=1.0, forgotten=1.0, preference=0.5
    )


def test_score_follows_the_weights_in_the_context():
    only_weather = Weights(colour=0, pattern=0, formality=0, weather=1, repetition=0, forgotten=0)

    total, _ = score(outfit(), make_ctx(weights=only_weather))

    assert total == pytest.approx(0.5)


def test_item_prescore_blends_closeness_weather_recency_and_forgotten():
    ctx = make_ctx(formality=(3, 3))

    # closeness 1, weather 0.5 (none), recency 0 -> 1, forgotten 1; weights .2 .2 .2 .05
    assert item_prescore(item(1, formality=3), ctx) == pytest.approx((0.2 * 1 + 0.2 * 0.5 + 0.2 * 1 + 0.05 * 1) / 0.65)


def test_item_prescore_falls_for_a_recent_wear_and_a_far_formality():
    ctx = make_ctx(formality=(3, 3), history=history(last_worn={1: days_ago(0)}))

    assert item_prescore(item(1, formality=3), ctx) < item_prescore(item(2, formality=3), ctx)
    assert item_prescore(item(2, formality=5), ctx) < item_prescore(item(2, formality=3), ctx)


def test_item_prescore_is_zero_when_its_four_weights_are_zero():
    weights = Weights(colour=1, pattern=1, formality=0, weather=0, repetition=0, forgotten=0)

    assert item_prescore(item(1), make_ctx(weights=weights)) == 0.0
