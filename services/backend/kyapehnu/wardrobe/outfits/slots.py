"""The places an item can fill in an outfit: seven that make it, and the finishing touches that ride along.

A blouse (an ethnic blouse or choli) is its own slot: it goes with a saree or lehenga and nothing else."""

import re
from enum import StrEnum
from typing import Final, cast

from kyapehnu.wardrobe.models import Item


class Slot(StrEnum):
    top = "top"
    bottom = "bottom"
    blouse = "blouse"
    full = "full"
    outerwear = "outerwear"
    footwear = "footwear"
    accessory = "accessory"
    watch = "watch"
    cap = "cap"
    sunglasses = "sunglasses"
    earrings = "earrings"
    necklace = "necklace"
    ring = "ring"
    bangles = "bangles"


SLOT_ORDER: Final = (
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
# Finishing touches: one of each goes with every outfit it fits, beside the one bag-or-other accessory slot.
EXTRA_SLOTS: Final = (Slot.watch, Slot.cap, Slot.sunglasses, Slot.earrings, Slot.necklace, Slot.ring, Slot.bangles)
JEWELLERY_SLOTS: Final = frozenset({Slot.earrings, Slot.necklace, Slot.ring, Slot.bangles})

SlottedItem = tuple[Slot, Item]

_CATEGORY_SLOT: Final = {
    "top": Slot.top,
    "bottom": Slot.bottom,
    "dress": Slot.full,
    "outerwear": Slot.outerwear,
    "footwear": Slot.footwear,
    "accessory": Slot.accessory,
    "bag": Slot.accessory,
}

# Checked in this order: the first slot with a whole-word match wins ("kurta pyjama" is a top).
_ETHNIC_SLOT_WORDS: Final = (
    (Slot.top, "kurtas?|kurtis?|kameez|sherwanis?"),
    (Slot.bottom, "churidars?|pyjamas?|pajamas?|salwars?|dhotis?|patialas?|shararas?|gararas?|palazzos?|leggings?"),
    (Slot.blouse, "blouses?|cholis?"),
    (Slot.full, "sarees?|saris?|lehengas?|lehngas?|ghagras?|anarkalis?|gowns?"),
    (Slot.outerwear, r"nehru\s+jacket|bandhgala|achkan|shrug"),
    (Slot.accessory, "dupatta|stole"),
)
_ETHNIC_PATTERNS: Final = tuple((slot, re.compile(rf"\b(?:{words})\b", re.I)) for slot, words in _ETHNIC_SLOT_WORDS)
# An accessory is a watch, a cap or sunglasses by a word in its subcategory or name; anything else stays an accessory.
_EXTRA_SLOT_WORDS: Final = (
    (Slot.watch, r"watch|smartwatch"),
    (Slot.cap, r"caps?|hats?|beanies?|snapbacks?|fedora|topi"),
    (Slot.sunglasses, r"sunglass(?:es)?|shades|aviators?|wayfarers?|eyewear|glasses|spectacles"),
    (Slot.earrings, r"earrings?|jhumkas?|jhumkis?|studs?|hoops?|chandbalis?|danglers?"),
    (Slot.necklace, r"necklaces?|chokers?|mangalsutras?|haars?|pendants?|chains?|neckpieces?"),
    (Slot.ring, r"rings?"),
    (Slot.bangles, r"bangles?|bracelets?|kadas?|cuffs?"),
)
# A full garment worn with a blouse of its own: a saree with its blouse, a lehenga with its choli.
_NEEDS_BLOUSE: Final = re.compile(r"\b(?:sarees?|saris?|lehengas?|lehngas?|ghagras?)\b", re.I)
_EXTRA_PATTERNS: Final = tuple((slot, re.compile(rf"\b(?:{words})\b", re.I)) for slot, words in _EXTRA_SLOT_WORDS)


def ethnic_slot(words: str) -> Slot:
    """The slot an ethnic garment's subcategory (or name) puts it in; a top when no word in it is known."""
    return next((slot for slot, pattern in _ETHNIC_PATTERNS if pattern.search(words)), Slot.top)


def extra_slot(words: str) -> Slot:
    """The slot an accessory's subcategory and name put it in; the accessory slot when no word in them is known."""
    return next((slot for slot, pattern in _EXTRA_PATTERNS if pattern.search(words)), Slot.accessory)


def pair_words(item: Item) -> str:
    """The words a garment's pairing is read from."""
    return f"{item.subcategory} {item.name}"


def needs_blouse(words: str) -> bool:
    """Whether a full garment with these words is worn with a blouse (a saree, a lehenga)."""
    return _NEEDS_BLOUSE.search(words) is not None


def slot_of(item: Item) -> Slot:
    if item.category == "ethnic":
        return ethnic_slot(item.subcategory)
    if item.category == "accessory":
        return extra_slot(f"{item.subcategory} {item.name}")
    return _CATEGORY_SLOT.get(item.category, Slot.top)


def item_id(item: Item) -> int:
    return cast(int, item.id)  # outfits only hold saved items
