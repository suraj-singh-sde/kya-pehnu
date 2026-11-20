"""Shoes follow the look: a family for the shoe and one for the outfit, a clash is skipped when another pair fits,
and the nearest pair stands in, with a note, when nothing in the wardrobe goes."""

from datetime import datetime

import pytest

from kyapehnu.core.config import WeatherPrefs
from kyapehnu.user.models import UserProfile
from kyapehnu.wardrobe.models import Event
from kyapehnu.wardrobe.outfits import pick
from kyapehnu.wardrobe.outfits.candidates import Candidate, build_pool, rank
from kyapehnu.wardrobe.outfits.footwear import FOOTWEAR_MISMATCH, footwear_notes, look_family, shoe_family, shoes_match
from kyapehnu.wardrobe.outfits.schemas import Parts
from kyapehnu.wardrobe.outfits.slots import Slot
from tests.outfit_fixtures import item, make_ctx

ANY = (1, 5)


def shoe(item_id, subcategory="", name="", **fields):
    return item(item_id, "footwear", subcategory=subcategory, name=name or f"item {item_id}", **fields)


def ranked(wardrobe):
    pool, problem = build_pool(wardrobe, ANY, None, WeatherPrefs())
    assert problem is None
    return rank(pool, make_ctx(), set(), per_slot=8, max_candidates=300)


def shoes_of(candidates):
    return {i.id for c in candidates for slot, i in c.items if slot is Slot.footwear}


@pytest.mark.parametrize(
    ("subcategory", "name", "family"),
    [
        ("juttis", "", "ethnic"),
        ("", "Red mojari", "ethnic"),
        ("kolhapuri chappals", "", "ethnic"),
        ("running shoes", "", "sport"),
        ("trainers", "", "sport"),
        ("", "Nike gym shoes", "sport"),
        ("oxfords", "", "formal"),
        ("", "Black derby", "formal"),
        ("brogues", "", "formal"),
        ("dress shoes", "", "formal"),
        ("sneakers", "", "casual"),
        ("canvas shoes", "", "casual"),
        ("", "White Dunk high-tops", "casual"),
        ("flip-flops", "", "casual"),
        ("loafers", "", "other"),
        ("sandals", "", "other"),
        ("heels", "", "other"),
        ("chelsea boots", "", "other"),
        ("", "", "other"),
    ],
)
def test_a_shoe_s_family_comes_from_a_whole_word_in_its_subcategory_or_name(subcategory, name, family):
    assert shoe_family(shoe(1, subcategory, name)) == family


def test_the_first_family_in_order_wins_so_ethnic_chappals_are_ethnic_and_sports_sandals_are_sport():
    assert shoe_family(shoe(1, "kolhapuri chappals")) == "ethnic"
    assert shoe_family(shoe(1, "sports sandals")) == "sport"


@pytest.mark.parametrize(
    ("look", "family"),
    [
        ([item(1, "ethnic", subcategory="kurta"), item(2, "bottom")], "ethnic"),
        ([item(1, "top", name="Linen kurta"), item(2, "bottom")], "ethnic"),
        ([item(1, "top", subcategory="jersey"), item(2, "bottom")], "sport"),
        ([item(1, "top"), item(2, "bottom", subcategory="track pants")], "sport"),
        ([item(1, "top", formality=4), item(2, "bottom", formality=4)], "formal"),
        ([item(1, "top", formality=4), item(2, "bottom", formality=2)], "formal"),
        ([item(1, "top", formality=3), item(2, "bottom", formality=3)], "casual"),
        ([item(1, "dress", formality=2)], "casual"),
    ],
    ids=[
        "ethnic category",
        "ethnic word",
        "sport top",
        "sport bottom",
        "tailoring",
        "one tailored piece",
        "smart casual",
        "a dress",
    ],
)
def test_a_look_s_family_is_ethnic_then_sport_by_its_words_then_formal_or_casual_by_its_highest_level(look, family):
    assert look_family(look) == family


def test_an_ethnic_word_wins_over_a_sport_word():
    assert look_family([item(1, "top", name="Sports kurta"), item(2, "bottom")]) == "ethnic"


SPORT = [item(1, "top", subcategory="jersey", formality=1), item(2, "bottom", subcategory="track pants", formality=1)]
KURTA = [item(1, "ethnic", subcategory="kurta", formality=2), item(2, "bottom", formality=2)]
SHERWANI = [
    item(1, "ethnic", subcategory="sherwani", formality=5),
    item(2, "ethnic", subcategory="churidar", formality=5),
]
SUIT = [item(1, "top", subcategory="shirt", formality=4), item(2, "bottom", subcategory="trousers", formality=4)]
TEE = [item(1, "top", subcategory="t-shirt", formality=2), item(2, "bottom", subcategory="shorts", formality=2)]
CHINOS = [item(1, "top", subcategory="shirt", formality=3), item(2, "bottom", subcategory="chinos", formality=3)]


@pytest.mark.parametrize(
    ("look", "kind", "ok"),
    [
        (SPORT, "oxfords", False),
        (SPORT, "juttis", False),
        (SPORT, "sneakers", True),
        (SPORT, "running shoes", True),
        (SPORT, "loafers", True),
        (KURTA, "running shoes", False),
        (KURTA, "sneakers", True),
        (KURTA, "juttis", True),
        (SHERWANI, "sneakers", False),
        (SHERWANI, "juttis", True),
        (SHERWANI, "oxfords", True),
        (SHERWANI, "sandals", True),
        (SUIT, "sneakers", False),
        (SUIT, "running shoes", False),
        (SUIT, "juttis", False),
        (SUIT, "oxfords", True),
        (SUIT, "loafers", True),
        (TEE, "oxfords", False),
        (TEE, "sneakers", True),
        (TEE, "juttis", True),
        (TEE, "running shoes", True),
        (CHINOS, "oxfords", True),
        (CHINOS, "sneakers", True),
    ],
    ids=lambda v: v if isinstance(v, str) else ("yes" if v is True else "no" if v is False else v[0].subcategory),
)
def test_which_shoes_a_look_takes(look, kind, ok):
    assert shoes_match(look, shoe(9, kind)) is ok


def test_outerwear_counts_towards_the_look_so_a_nehru_jacket_makes_jeans_ethnic():
    assert (
        shoes_match([*TEE, item(3, "outerwear", subcategory="nehru jacket", formality=3)], shoe(9, "running shoes"))
        is False
    )


def test_clashing_shoes_are_left_out_when_another_pair_fits_the_look():
    wardrobe = [*SHERWANI, shoe(3, "running shoes"), shoe(4, "juttis")]

    candidates = ranked(wardrobe)

    assert candidates and shoes_of(candidates) == {4}
    assert all(c.notes == () for c in candidates)


def test_the_only_pair_stands_in_with_a_note_when_nothing_fits_the_look():
    wardrobe = [*SHERWANI, shoe(3, "running shoes")]

    candidates = ranked(wardrobe)

    assert candidates and shoes_of(candidates) == {3}
    assert all(c.notes == (FOOTWEAR_MISMATCH,) for c in candidates)


def test_each_look_keeps_its_own_fitting_shoes():
    tee, shorts = (
        item(1, "top", subcategory="t-shirt", formality=2),
        item(2, "bottom", subcategory="shorts", formality=2),
    )
    sherwani = item(5, "ethnic", subcategory="sherwani", formality=5)
    churidar = item(6, "ethnic", subcategory="churidar", formality=5)
    wardrobe = [tee, shorts, sherwani, churidar, shoe(3, "sneakers"), shoe(4, "juttis")]

    candidates = ranked(wardrobe)

    by_top = {}
    for c in candidates:
        top = next(i.id for slot, i in c.items if slot is Slot.top)
        by_top.setdefault(top, set()).update(i.id for slot, i in c.items if slot is Slot.footwear)
    assert by_top[1] == {3, 4}  # sneakers or juttis under a tee
    assert by_top[5] == {4}  # only juttis under the sherwani


def test_the_mismatch_note_follows_the_pool_notes_and_an_outfit_without_shoes_has_none():
    assert footwear_notes(((Slot.top, item(1)), (Slot.bottom, item(2)))) == ()
    assert footwear_notes(((Slot.top, SUIT[0]), (Slot.bottom, SUIT[1]), (Slot.footwear, shoe(3, "sneakers")))) == (
        FOOTWEAR_MISMATCH,
    )


def test_the_pick_prompt_says_which_candidates_shoes_stand_in():
    parts = Parts(colour=0.5, pattern=0.5, formality=0.5, weather=0.5, repetition=0.5, forgotten=0.5, preference=0.5)
    items = ((Slot.top, SUIT[0]), (Slot.bottom, SUIT[1]), (Slot.footwear, shoe(3, "sneakers")))
    marked = Candidate(items, (FOOTWEAR_MISMATCH,), 0.6, parts)
    plain = Candidate(items, (), 0.5, parts)
    event = Event(id=1, type="office", starts_at=datetime(2026, 10, 10, 9), place="Gurugram")

    prompt = pick.build_prompt(event, None, UserProfile(), [marked, plain])

    lines = [line for line in prompt.splitlines() if line.startswith("#")]
    assert lines[0].endswith(pick.MISMATCH_SUFFIX) and not lines[1].endswith(pick.MISMATCH_SUFFIX)
    assert "Footwear belongs to the look" in prompt
