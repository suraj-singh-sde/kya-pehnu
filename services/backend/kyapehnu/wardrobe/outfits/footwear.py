"""Which shoes go with which look. Formality numbers cannot tell running shoes from juttis, so a look and a pair of
shoes each get a family from their words, and a clash (trainers under a sherwani, oxfords with shorts) is skipped.

Families of a look: ethnic, sport, formal (tailoring at dress level 4 or more), casual. Families of a shoe: ethnic,
sport, formal, casual (sneakers, canvas, flip-flops) and other (loafers, boots, sandals, heels: they go with most).
"""

import re
from collections.abc import Sequence
from typing import Final

from kyapehnu.wardrobe.models import Item
from kyapehnu.wardrobe.outfits.slots import Slot, SlottedItem

FOOTWEAR_MISMATCH: Final = "footwear_mismatch"
# The pieces that set a look's family and level: not the bag, the watch or the sunglasses.
LOOK_SLOTS: Final = frozenset({Slot.top, Slot.bottom, Slot.full, Slot.outerwear})
OTHER: Final = "other"

# Checked in this order: the first family with a whole-word match wins ("kolhapuri chappals" are ethnic).
_SHOE_WORDS: Final = (
    ("ethnic", r"juttis?|jootis?|jutis?|mojaris?|mojris?|kolhapuris?|peshawaris?|nagras?|khussas?"),
    (
        "sport",
        r"running|runners?|trainers?|sports?|gym|training|athletic|basketball|football|tennis|cleats?|turf|spikes?",
    ),
    ("formal", r"oxfords?|derby|derbies|brogues?|monks?|monk-?straps?|wholecuts?|dress|formal|court"),
    (
        "casual",
        r"sneakers?|canvas|slip-?ons?|plimsolls?|high-?tops?|low-?tops?|skate|flip-?flops?|sliders?|slides|crocs|chappals?",
    ),
)
_SHOE_PATTERNS: Final = tuple((family, re.compile(rf"\b(?:{words})\b", re.I)) for family, words in _SHOE_WORDS)
_ETHNIC_LOOK: Final = re.compile(
    r"\b(?:kurtas?|kurtis?|sherwanis?|bandhgalas?|nehru|sarees?|saris?|lehengas?|anarkalis?|churidars?|salwars?|dhotis?"
    r"|pathanis?|achkans?|cholis?|shararas?)\b",
    re.I,
)
_SPORT_LOOK: Final = re.compile(
    r"\b(?:tracksuits?|track\s*pants?|joggers?|gym|jerseys?|sports?|athletic|training|running|sweatpants?|leggings?"
    r"|yoga|cycling|workout|activewear)\b",
    re.I,
)
# Shoe families a look never takes, whatever the level.
_BLOCKED: Final = {
    "sport": frozenset({"ethnic", "formal"}),
    "ethnic": frozenset({"sport"}),
    "formal": frozenset({"sport", "ethnic", "casual"}),
    "casual": frozenset[str](),
}
SNEAKER_LEVEL_MAX: Final = 3  # sneakers under a kurta at a casual level, never under wedding-level ethnic wear
FORMAL_SHOE_LEVEL_MIN: Final = 3  # oxfords from smart casual up, not with a tee and shorts


def shoe_family(shoe: Item) -> str:
    words = f"{shoe.subcategory} {shoe.name}"
    return next((family for family, pattern in _SHOE_PATTERNS if pattern.search(words)), OTHER)


def look_family(look: Sequence[Item]) -> str:
    """The family of the pieces that make the look (top, bottom or full, and outerwear). Ethnic wins over sport."""
    words = " ".join(f"{i.subcategory} {i.name}" for i in look)
    if any(i.category == "ethnic" for i in look) or _ETHNIC_LOOK.search(words):
        return "ethnic"
    if _SPORT_LOOK.search(words):
        return "sport"
    return "formal" if max(i.formality for i in look) >= 4 else "casual"


def shoes_match(look: Sequence[Item], shoe: Item) -> bool:
    family, kind, level = look_family(look), shoe_family(shoe), max(i.formality for i in look)
    if kind in _BLOCKED[family]:
        return False
    if family == "ethnic" and kind == "casual":
        return level <= SNEAKER_LEVEL_MAX
    if family == "casual" and kind == "formal":
        return level >= FORMAL_SHOE_LEVEL_MIN
    return True


def footwear_notes(items: Sequence[SlottedItem]) -> tuple[str, ...]:
    """The mismatch note when an outfit's shoes do not go with its look: the nearest pair stood in for none."""
    look = [i for slot, i in items if slot in LOOK_SLOTS]
    shoe = next((i for slot, i in items if slot is Slot.footwear), None)
    return (FOOTWEAR_MISMATCH,) if shoe is not None and look and not shoes_match(look, shoe) else ()
