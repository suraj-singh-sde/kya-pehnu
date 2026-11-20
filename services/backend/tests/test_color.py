import pytest

from kyapehnu.wardrobe.color import delta_e, harmony, hex_to_lab, hue_pair_score, is_neutral, ryb_hue


def test_white_and_black_lab():
    assert hex_to_lab("#ffffff") == pytest.approx((100, 0, 0), abs=0.5)
    assert hex_to_lab("#000000") == pytest.approx((0, 0, 0), abs=0.5)


def test_delta_e():
    assert delta_e("#1F2A44", "#1F2A44") == 0
    assert delta_e("#1F2A44", "#22335A") < 15  # two navies
    assert delta_e("#ff0000", "#00ff00") > 100


def colour(hex_: str, name: str = "x") -> dict[str, str]:
    return {"hex": hex_, "name": name}


RED, ORANGE, AMBER, YELLOW, GREEN, BLUE = "#ff0000", "#ff8000", "#ffbf00", "#ffff00", "#00ff00", "#0000ff"


@pytest.mark.parametrize("name", ["Navy", "off white", "BLACK", "Dark Grey", "gray", "denim", "Khaki", "ivory"])
def test_neutral_by_whole_word_in_the_name(name):
    assert is_neutral(colour(RED, name))


def test_name_containing_a_neutral_only_as_a_substring_is_not_neutral():
    assert not is_neutral(colour(RED, "whitewash"))


@pytest.mark.parametrize(("hex_", "expected"), [("#6e8092", True), ("#6c8094", False), ("#808080", True)])
def test_neutral_by_saturation_below_015(hex_, expected):
    assert is_neutral(colour(hex_, "slate")) is expected


@pytest.mark.parametrize(
    ("hex_", "degrees"),
    [(RED, 0), (ORANGE, 60.2), (YELLOW, 120), (GREEN, 180), (BLUE, 240), ("#ff00ff", 300), (AMBER, 90)],
)
def test_ryb_hue_maps_rgb_hue_onto_the_artist_wheel(hex_, degrees):
    assert ryb_hue(hex_) == pytest.approx(degrees, abs=0.5)


@pytest.mark.parametrize(
    ("distance", "score"),
    [
        (0, 1.0),
        (74.9, 1.0),
        (75, 0.3),
        (104.9, 0.3),
        (105, 0.6),
        (134.9, 0.6),
        (135, 0.3),
        (149.9, 0.3),
        (150, 1.0),
        (180, 1.0),
    ],
)
def test_hue_pair_score_at_the_band_boundaries(distance, score):
    assert hue_pair_score(distance) == score


def test_navy_white_and_one_colour_is_high():
    assert harmony([colour(RED, "navy"), colour(RED, "white"), colour(RED, "red")]) == 1.0


def test_fewer_than_two_non_neutrals_is_high():
    assert harmony([colour(RED)]) == 1.0
    assert harmony([]) == 1.0


def test_red_with_its_opposite_green_is_high():
    assert harmony([colour(RED), colour(GREEN)]) == 1.0


def test_red_with_neighbouring_orange_is_high():
    assert harmony([colour(RED), colour(ORANGE)]) == 1.0


def test_hues_120_degrees_apart_score_mid():
    assert harmony([colour(RED), colour(YELLOW)]) == 0.6


def test_hues_90_degrees_apart_score_low():
    assert harmony([colour(RED), colour(AMBER)]) == 0.3


def test_the_worst_pair_decides():
    assert harmony([colour(RED), colour(ORANGE), colour(AMBER)]) == 0.3


def test_three_distinct_hue_buckets_are_still_scored_by_pairs():
    assert harmony([colour(RED), colour(YELLOW), colour(BLUE)]) == 0.6


def test_four_distinct_hue_buckets_score_zero():
    assert harmony([colour(RED), colour(YELLOW), colour(GREEN), colour(BLUE)]) == 0.0


def test_two_colours_in_one_bucket_count_once():
    assert harmony([colour(RED), colour("#ff1000"), colour(YELLOW), colour(BLUE), colour(GREEN)]) == 0.0
    assert harmony([colour(RED), colour("#ff1000"), colour(YELLOW), colour(BLUE)]) == 0.6
