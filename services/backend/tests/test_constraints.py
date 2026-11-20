import pytest

from kyapehnu.core.config import WeatherBand, WeatherPrefs
from kyapehnu.wardrobe.outfits.constraints import derive, is_marked_down, is_rain_safe
from kyapehnu.weather.summary import WeatherSummary
from tests.outfit_fixtures import constraints, item

PREFS = WeatherPrefs()
HUMID_NOTE = "Hot and humid: breathable fabrics, no layers."
RAIN_NOTE = "Rain likely: rain-safe footwear."
SUN_NOTE = "Strong sun: cover up, cap or sunglasses."
AIR_NOTE = "Poor air quality: a mask-friendly outfit."
WIND_NOTE = "Windy: avoid loose layers."


def summary(feels_like: float = 20, **fields: float | None) -> WeatherSummary:
    values = {
        "source": "forecast",
        "feels_like_min_c": feels_like,
        "feels_like_max_c": feels_like,
        "humidity_mean_pct": None,
        "rain_probability_max_pct": None,
        "wind_max_kmh": None,
        "uv_max": None,
        "aqi_max": None,
    }
    return WeatherSummary(**{**values, **fields})


@pytest.mark.parametrize(
    ("mid", "warmth", "layer"),
    [
        (30, (1, 2), "none"),
        (29.9, (1, 3), "none"),
        (24, (1, 3), "none"),
        (23.9, (2, 4), "optional"),
        (18, (2, 4), "optional"),
        (17.9, (3, 5), "wanted"),
        (10, (3, 5), "wanted"),
        (9.9, (4, 5), "wanted"),
        (-5, (4, 5), "wanted"),
    ],
)
def test_band_and_layer_at_every_boundary(mid, warmth, layer):
    cons = derive(summary(mid), outdoors=False, prefs=PREFS)

    assert ((cons.warmth_min, cons.warmth_max), cons.layer) == (warmth, layer)


def test_the_band_follows_the_midpoint_of_the_feels_like_range():
    cons = derive(summary(feels_like_min_c=20, feels_like_max_c=36), outdoors=False, prefs=PREFS)  # mid 28

    assert (cons.warmth_min, cons.warmth_max) == (1, 3)


def test_bands_come_from_the_prefs():
    prefs = WeatherPrefs(
        bands=[
            WeatherBand(from_c=15, warmth_min=1, warmth_max=1, layer="none"),
            WeatherBand(warmth_min=5, warmth_max=5, layer="wanted"),
        ]
    )

    assert derive(summary(15), False, prefs).layer == "none"
    assert derive(summary(14.9), False, prefs).layer == "wanted"


@pytest.mark.parametrize(
    ("humidity", "feels_like", "fires"),
    [(70, 28, True), (69.9, 28, False), (70, 27.9, False), (None, 30, False), (100, 40, True)],
)
def test_hot_and_humid_marks_fabrics_down_at_its_thresholds(humidity, feels_like, fires):
    cons = derive(summary(feels_like, humidity_mean_pct=humidity), outdoors=False, prefs=PREFS)

    assert (HUMID_NOTE in cons.notes) is fires
    assert cons.marked_down == (frozenset(PREFS.humid_fabrics) if fires else frozenset())


def test_marked_down_fabrics_are_the_configured_ones_casefolded():
    prefs = WeatherPrefs(humid_fabrics=["Linen", "RAYON"])

    cons = derive(summary(30, humidity_mean_pct=80), outdoors=False, prefs=prefs)

    assert cons.marked_down == frozenset({"linen", "rayon"})


@pytest.mark.parametrize(("rain", "likely"), [(41, True), (40.1, True), (40, False), (0, False), (None, False)])
def test_rain_is_likely_above_the_threshold_only(rain, likely):
    cons = derive(summary(rain_probability_max_pct=rain), outdoors=False, prefs=PREFS)

    assert cons.rain_likely is likely
    assert (RAIN_NOTE in cons.notes) is likely


@pytest.mark.parametrize(
    ("uv", "outdoors", "fires"), [(8, True, True), (7.9, True, False), (9, False, False), (None, True, False)]
)
def test_strong_sun_needs_outdoors_and_uv_at_the_threshold(uv, outdoors, fires):
    cons = derive(summary(uv_max=uv), outdoors=outdoors, prefs=PREFS)

    assert (SUN_NOTE in cons.notes) is fires


@pytest.mark.parametrize(
    ("aqi", "outdoors", "fires"), [(201, True, True), (200, True, False), (500, False, False), (None, True, False)]
)
def test_poor_air_needs_outdoors_and_aqi_above_the_threshold(aqi, outdoors, fires):
    cons = derive(summary(aqi_max=aqi), outdoors=outdoors, prefs=PREFS)

    assert (AIR_NOTE in cons.notes) is fires


@pytest.mark.parametrize(("wind", "fires"), [(36, True), (35.1, True), (35, False), (None, False)])
def test_wind_note_above_the_threshold_indoors_or_out(wind, fires):
    cons = derive(summary(wind_max_kmh=wind), outdoors=False, prefs=PREFS)

    assert (WIND_NOTE in cons.notes) is fires


def test_thresholds_come_from_the_prefs():
    prefs = WeatherPrefs(rain_probability_pct=10, uv_index=3, aqi=50, wind_kmh=10)

    cons = derive(summary(rain_probability_max_pct=11, uv_max=3, aqi_max=51, wind_max_kmh=11), True, prefs)

    assert cons.notes == (RAIN_NOTE, SUN_NOTE, AIR_NOTE, WIND_NOTE)


def test_notes_come_in_rule_order():
    cons = derive(
        summary(
            30,
            humidity_mean_pct=90,
            rain_probability_max_pct=90,
            uv_max=11,
            aqi_max=300,
            wind_max_kmh=60,
        ),
        outdoors=True,
        prefs=PREFS,
    )

    assert cons.notes == (HUMID_NOTE, RAIN_NOTE, SUN_NOTE, AIR_NOTE, WIND_NOTE)


def test_a_quiet_day_has_no_notes():
    assert derive(summary(20), outdoors=True, prefs=PREFS).notes == ()


@pytest.mark.parametrize(
    ("fabric", "marked"),
    [
        ("polyester", True),
        ("Polyester blend", True),
        ("POLYESTER", True),
        ("wool", True),
        ("woolen", False),
        ("cotton", False),
        (None, False),
    ],
)
def test_marked_down_matches_whole_fabric_words(fabric, marked):
    cons = constraints(marked_down=frozenset(PREFS.humid_fabrics))

    assert is_marked_down(item(1, fabric=fabric), cons) is marked


def test_nothing_is_marked_down_without_marked_fabrics():
    assert not is_marked_down(item(1, fabric="polyester"), constraints())


@pytest.mark.parametrize(
    "fields",
    [
        {"fabric": "Suede"},
        {"name": "Canvas sneakers"},
        {"subcategory": "silk slipper"},
        {"tags": ["party", "Mesh"]},
        {"fabric": "satin"},
        {"fabric": "velvet"},
    ],
)
def test_footwear_with_a_not_rain_safe_word_is_not_rain_safe(fields):
    assert not is_rain_safe(item(1, "footwear", **fields), PREFS.not_rain_safe_words)


def test_leather_footwear_is_rain_safe():
    assert is_rain_safe(item(1, "footwear", fabric="leather"), PREFS.not_rain_safe_words)


def test_rain_safe_words_match_whole_words_only():
    assert is_rain_safe(item(1, "footwear", name="Suedehead boots"), PREFS.not_rain_safe_words)


def test_everything_is_rain_safe_with_no_words():
    assert is_rain_safe(item(1, "footwear", fabric="suede"), [])
