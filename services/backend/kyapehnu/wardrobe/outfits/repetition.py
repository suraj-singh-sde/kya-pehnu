"""How much an outfit repeats what was worn before (item recency, same outfit, same audience) and what was forgotten."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from kyapehnu.core.config import RepetitionPrefs
from kyapehnu.wardrobe.models import Item
from kyapehnu.wardrobe.outfits.slots import SLOT_ORDER, Slot, SlottedItem, item_id

EXACT_OUTFIT_PENALTY: Final = 0.8
AUDIENCE_SAME_OUTFIT_PENALTY: Final = 0.9
AUDIENCE_STANDOUT_PENALTY: Final = 0.5
PATTERN_BOLDNESS: Final = {"solid": 0, "stripe": 1, "check": 2, "other": 2, "embroidered": 3, "print": 3, "floral": 4}


@dataclass(frozen=True)
class History:
    last_worn: dict[int, date]  # item id -> latest worn_on, every wear
    outfit_last_worn: dict[frozenset[int], date]  # outfit wears only
    audience_wears: list[
        tuple[frozenset[int], date]
    ]  # outfit wears in the audience window that share a name with the event


def _days_since(last: date, today: date) -> int:
    return max(0, (today - last).days)


def item_recency(last: date | None, today: date, half_life_days: float) -> float:
    return 0.0 if last is None else 0.5 ** (_days_since(last, today) / half_life_days)


def exact_outfit(last: date | None, today: date, window_days: int) -> float:
    if last is None:
        return 0.0
    return EXACT_OUTFIT_PENALTY * max(0.0, 1 - _days_since(last, today) / window_days)


def audience(ids: frozenset[int], standout_id: int, wears: Sequence[tuple[frozenset[int], date]]) -> float:
    def penalty(worn: frozenset[int]) -> float:
        if worn == ids:
            return AUDIENCE_SAME_OUTFIT_PENALTY
        return AUDIENCE_STANDOUT_PENALTY if standout_id in worn else 0.0

    return max((penalty(worn) for worn, _ in wears), default=0.0)


def standout(items: Sequence[SlottedItem]) -> Item:
    """The piece people notice: the outerwear, else the boldest pattern (the earlier slot on a tie)."""
    return min(
        items,
        key=lambda si: (si[0] is not Slot.outerwear, -PATTERN_BOLDNESS.get(si[1].pattern, 0), SLOT_ORDER.index(si[0])),
    )[1]


def repetition_part(items: Sequence[SlottedItem], history: History, today: date, prefs: RepetitionPrefs) -> float:
    ids = frozenset(item_id(i) for _, i in items)
    recency = sum(item_recency(history.last_worn.get(i), today, prefs.half_life_days) for i in ids) / len(ids)
    exact = exact_outfit(history.outfit_last_worn.get(ids), today, prefs.outfit_window_days)
    same_audience = audience(ids, item_id(standout(items)), history.audience_wears)
    return 1 - min(1.0, recency + exact + same_audience)


def is_forgotten(last: date | None, today: date, days: int) -> bool:
    return last is None or (today - last).days >= days


def forgotten_part(items: Sequence[Item], last_worn: Mapping[int, date], today: date, days: int) -> float:
    if not items:
        return 0.0
    return sum(is_forgotten(last_worn.get(item_id(i)), today, days) for i in items) / len(items)
