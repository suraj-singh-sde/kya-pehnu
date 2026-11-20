"""Builds the pool of usable items for an event, then ranks the outfits that can be made from it."""

import heapq
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from typing import Final

from kyapehnu.core.config import WeatherPrefs
from kyapehnu.wardrobe.models import Item, ItemStatus
from kyapehnu.wardrobe.outfits.constraints import Constraints, is_rain_safe
from kyapehnu.wardrobe.outfits.footwear import footwear_notes, shoes_match
from kyapehnu.wardrobe.outfits.schemas import Parts, Problem
from kyapehnu.wardrobe.outfits.scoring import ScoreContext, item_prescore, score
from kyapehnu.wardrobe.outfits.slots import (
    EXTRA_SLOTS,
    JEWELLERY_SLOTS,
    SLOT_ORDER,
    Slot,
    SlottedItem,
    item_id,
    needs_blouse,
    pair_words,
    slot_of,
)

NO_FOOTWEAR: Final = "no_footwear"
# Warmth means nothing for these: a watch or sunglasses goes with a winter coat as well as a summer shirt.
UNWARMED: Final = frozenset({Slot.watch, Slot.sunglasses, *JEWELLERY_SLOTS})
FOOTWEAR_NOT_RAIN_SAFE: Final = "footwear_not_rain_safe"
NO_BLOUSE: Final = "no_blouse"  # a saree or lehenga was left out: no blouse or choli fits to go with it
NO_BLOUSE_MESSAGE: Final = "Add a blouse to go with your saree to get outfit suggestions"

__all__ = ["Candidate", "Pool", "SlottedItem", "apply_include", "build_pool", "keeps_pair", "rank"]


@dataclass(frozen=True)
class Pool:
    by_slot: dict[Slot, list[Item]]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    items: tuple[SlottedItem, ...]
    notes: tuple[str, ...]
    score: float
    parts: Parts

    @property
    def item_ids(self) -> list[int]:
        return [item_id(i) for _, i in self.items]

    @property
    def ids(self) -> frozenset[int]:
        return frozenset(self.item_ids)


def _fits(item: Item, slot: Slot, formality: tuple[int, int], cons: Constraints | None) -> bool:
    if slot is Slot.blouse:  # worn under its saree: the saree's formality and warmth are the outfit's
        return item.status == "available"
    ok = item.status == "available" and formality[0] <= item.formality <= formality[1]
    return ok and (cons is None or slot in UNWARMED or cons.warmth_min <= item.warmth <= cons.warmth_max)


def _footwear(
    footwear: list[Item], wardrobe: Sequence[Item], cons: Constraints | None, prefs: WeatherPrefs
) -> tuple[list[Item], tuple[str, ...]]:
    """The shoes an outfit may take. When no pair fits the dress level and the weather, every available pair stands
    in: the nearest shoes are better than none (the family rule and its mismatch note still apply per look)."""
    if not footwear:
        footwear = [i for i in wardrobe if slot_of(i) is Slot.footwear and i.status == ItemStatus.available]
    if not footwear:
        return footwear, (NO_FOOTWEAR,)
    if cons is None or not cons.rain_likely:
        return footwear, ()
    safe = [f for f in footwear if is_rain_safe(f, prefs.not_rain_safe_words)]
    return (safe, ()) if safe else (footwear, (FOOTWEAR_NOT_RAIN_SAFE,))


def _needs_blouse(item: Item) -> bool:
    return needs_blouse(pair_words(item))


def keeps_pair(spare: Item, slot: Slot, held: frozenset[Slot]) -> bool:
    """Whether swapping `spare` into `slot` keeps the look whole: a saree only where a blouse is held, a dress only
    where none is. Every other slot swaps freely."""
    return slot is not Slot.full or _needs_blouse(spare) == (Slot.blouse in held)


def _with_blouses(by_slot: dict[Slot, list[Item]]) -> tuple[str, ...]:
    """Takes out every saree or lehenga when no blouse fits to go with it; the note says so."""
    fulls = by_slot[Slot.full]
    if by_slot[Slot.blouse] or not any(_needs_blouse(f) for f in fulls):
        return ()
    by_slot[Slot.full] = [f for f in fulls if not _needs_blouse(f)]
    return (NO_BLOUSE,)


def _problem(wardrobe: Sequence[Item], by_slot: dict[Slot, list[Item]], notes: tuple[str, ...]) -> Problem | None:
    if (by_slot[Slot.top] and by_slot[Slot.bottom]) or by_slot[Slot.full]:
        return None
    if NO_BLOUSE in notes:
        return Problem(slot=Slot.blouse, cause="missing", message=NO_BLOUSE_MESSAGE)
    slot = Slot.top if not by_slot[Slot.top] else Slot.bottom
    if not any(slot_of(i) is slot for i in wardrobe):
        return Problem(slot=slot, cause="missing", message=f"Add at least one {slot} to get outfit suggestions")
    return Problem(
        slot=slot,
        cause="filtered",
        message=f"No {slot} fits this event: every {slot} was filtered out by status, formality or weather",
    )


def build_pool(
    wardrobe: Sequence[Item], formality: tuple[int, int], cons: Constraints | None, prefs: WeatherPrefs
) -> tuple[Pool, Problem | None]:
    """Group the usable items by slot; the problem says why no outfit can be made, if so."""
    by_slot: dict[Slot, list[Item]] = {slot: [] for slot in SLOT_ORDER}
    for item in wardrobe:
        slot = slot_of(item)
        if _fits(item, slot, formality, cons):
            by_slot[slot].append(item)
    if cons is not None and cons.layer == "none":
        by_slot[Slot.outerwear] = []
    by_slot[Slot.footwear], notes = _footwear(by_slot[Slot.footwear], wardrobe, cons, prefs)
    notes += _with_blouses(by_slot)
    return Pool(by_slot, notes), _problem(wardrobe, by_slot, notes)


def _outerwear_options(outerwear: list[Item], cons: Constraints | None) -> list[Item | None]:
    if cons is None or cons.layer == "optional":
        return [None, *outerwear]
    if cons.layer == "wanted":
        return [*outerwear] or [None]
    return [None]


def _matching_shoes(look: Sequence[Item], footwear: Sequence[Item | None]) -> frozenset[int] | None:
    """The ids of the shoes that go with this look; None when no pair does, so every pair stays in: the nearest
    shoes are better than none, and the candidate carries the mismatch note."""
    return frozenset(item_id(s) for s in footwear if s is not None and shoes_match(look, s)) or None


def _expand(
    shortlist: dict[Slot, list[Item]],
    cons: Constraints | None,
    excluded: set[frozenset[int]],
    required: frozenset[Slot],
) -> Iterator[tuple[SlottedItem, ...]]:
    """Every outfit of the shortlists. A required slot holds its one included item: it has no "none" option.
    A watch, cap or sunglasses that fits rides along with every outfit (one of each, taken in turn so the picks
    differ) instead of competing for the accessory slot. Shoes that clash with the look (trainers under a sherwani,
    oxfords with shorts) are left out whenever another pair fits it."""
    footwear: list[Item | None] = list(shortlist[Slot.footwear]) or [None]
    accessories: list[Item | None] = [*shortlist[Slot.accessory]]
    if Slot.accessory not in required:
        accessories.insert(0, None)
    outerwear: list[Item | None] = (
        [*shortlist[Slot.outerwear]]
        if Slot.outerwear in required
        else _outerwear_options(shortlist[Slot.outerwear], cons)
    )
    bases: list[tuple[SlottedItem, ...]] = [
        ((Slot.top, top), (Slot.bottom, bottom)) for top, bottom in product(shortlist[Slot.top], shortlist[Slot.bottom])
    ]
    for full in shortlist[Slot.full]:  # a saree or lehenga goes with each blouse that fits, a dress on its own
        if _needs_blouse(full):
            bases += [((Slot.blouse, blouse), (Slot.full, full)) for blouse in shortlist[Slot.blouse]]
        else:
            bases.append(((Slot.full, full),))
    riders = {slot: shortlist[slot] for slot in EXTRA_SLOTS if shortlist[slot]}
    fitting: dict[tuple[int, int | None], frozenset[int] | None] = {}  # per base and coat: the shoes that go
    for n, ((b, base), shoes, coat, extra) in enumerate(product(enumerate(bases), footwear, outerwear, accessories)):
        key = (b, None if coat is None else item_id(coat))
        if key not in fitting:
            fitting[key] = _matching_shoes([i for _, i in base] + ([coat] if coat is not None else []), footwear)
        fits = fitting[key]
        if shoes is not None and fits is not None and item_id(shoes) not in fits:
            continue
        extras = tuple(
            (slot, i)
            for slot, i in ((Slot.outerwear, coat), (Slot.footwear, shoes), (Slot.accessory, extra))
            if i is not None
        )
        items = base + extras + tuple((slot, choices[n % len(choices)]) for slot, choices in riders.items())
        if frozenset(item_id(i) for _, i in items) not in excluded:
            yield items


def _must_include(slot: Slot | None, message: str) -> Problem:
    return Problem(slot=slot, cause="must_include", message=message)


def _clash(item: Item, slot: Slot, chosen: dict[Slot, Item]) -> Problem | None:
    """The later of two included items that cannot share an outfit: two of one slot, or a full outfit and a top or
    bottom."""
    if slot in chosen:
        return _must_include(slot, f"{chosen[slot].name} and {item.name} are both {slot}s: an outfit holds one")
    halves = [chosen[s] for s in (Slot.top, Slot.bottom) if s in chosen]
    if slot is Slot.full and halves:
        return _must_include(slot, f"{item.name} is a full outfit and cannot go with {halves[0].name}")
    if slot in (Slot.top, Slot.bottom) and Slot.full in chosen:
        return _must_include(slot, f"{chosen[Slot.full].name} is a full outfit and cannot go with {item.name}")
    # A blouse goes with a saree or lehenga only; a dress is complete without one.
    if slot is Slot.blouse and (halves or (Slot.full in chosen and not _needs_blouse(chosen[Slot.full]))):
        other = halves[0] if halves else chosen[Slot.full]
        return _must_include(slot, f"{item.name} only goes with a saree or lehenga, not with {other.name}")
    if slot in (Slot.top, Slot.bottom) and Slot.blouse in chosen:
        return _must_include(
            slot, f"{chosen[Slot.blouse].name} only goes with a saree or lehenga, not with {item.name}"
        )
    if slot is Slot.full and Slot.blouse in chosen and not _needs_blouse(item):
        return _must_include(
            slot, f"{chosen[Slot.blouse].name} only goes with a saree or lehenga, not with {item.name}"
        )
    return None


def _unusable(item: Item, pool: Pool) -> str | None:
    """Why an included item cannot be worn at all. The dress level and the weather are not reasons: the wearer chose
    the piece, and the rules dress the rest of the look around it."""
    if item.status != ItemStatus.available:
        return f"{item.name} is not available ({item.status})"
    if slot_of(item) is Slot.full and _needs_blouse(item) and NO_BLOUSE in pool.notes:
        return f"{item.name} has no blouse to go with it"
    return None


def apply_include(
    pool: Pool, include: Sequence[int], items_by_id: Mapping[int, Item]
) -> tuple[Pool, frozenset[Slot], Problem | None]:
    """Make every outfit hold the included items: each included slot is reduced to its item. The first item that cannot
    be included, in the order given, is named in the problem (then the pool comes back unchanged)."""
    chosen: dict[Slot, Item] = {}
    for wanted in dict.fromkeys(include):
        if (item := items_by_id.get(wanted)) is None:
            return pool, frozenset(), _must_include(None, f"Item {wanted} is not in your wardrobe")
        slot = slot_of(item)
        if message := _unusable(item, pool):
            return pool, frozenset(), _must_include(slot, message)
        if problem := _clash(item, slot, chosen):
            return pool, frozenset(), problem
        chosen[slot] = item
    by_slot = dict(pool.by_slot)
    for slot, item in chosen.items():
        by_slot[slot] = [item]
    if Slot.full in chosen:
        by_slot[Slot.top] = by_slot[Slot.bottom] = []
        if not _needs_blouse(chosen[Slot.full]):
            by_slot[Slot.blouse] = []
    elif Slot.blouse in chosen:
        by_slot[Slot.top] = by_slot[Slot.bottom] = []
        by_slot[Slot.full] = [f for f in by_slot[Slot.full] if _needs_blouse(f)]
        if not by_slot[Slot.full]:
            message = f"{chosen[Slot.blouse].name} has no saree or lehenga to go with it: none fits this event"
            return pool, frozenset(), _must_include(Slot.full, message)
    elif chosen.keys() & {Slot.top, Slot.bottom}:
        by_slot[Slot.full] = []
        for slot, partner in ((Slot.top, Slot.bottom), (Slot.bottom, Slot.top)):
            if slot in chosen and not by_slot[partner]:
                message = f"{chosen[slot].name} has no {partner} to go with it: none fits this event"
                return pool, frozenset(), _must_include(partner, message)
    return Pool(by_slot, pool.notes), frozenset(chosen), None


def rank(
    pool: Pool,
    ctx: ScoreContext,
    excluded: set[frozenset[int]],
    per_slot: int,
    max_candidates: int,
    required: frozenset[Slot] = frozenset(),
) -> list[Candidate]:
    """The best `max_candidates` outfits by item pre-score, fully scored, best first. Excluded sets are never built.
    A slot in `required` always holds its one item (see `apply_include`)."""
    prescore = {item_id(i): item_prescore(i, ctx) for items in pool.by_slot.values() for i in items}
    shortlist = {
        slot: sorted(items, key=lambda i: (-prescore[item_id(i)], item_id(i)))[:per_slot]
        for slot, items in pool.by_slot.items()
    }
    best = heapq.nlargest(
        max_candidates,
        _expand(shortlist, ctx.cons, excluded, required),
        key=lambda items: sum(prescore[item_id(i)] for _, i in items) / len(items),
    )
    candidates = [Candidate(items, pool.notes + footwear_notes(items), *score(items, ctx)) for items in best]
    return sorted(candidates, key=lambda c: c.score, reverse=True)
