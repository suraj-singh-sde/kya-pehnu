"""Factories for the pure outfit-rule tests: items and contexts built in memory, no database."""

from datetime import date
from typing import Any

from kyapehnu.core.config import RepetitionPrefs, Weights
from kyapehnu.wardrobe.models import Item
from kyapehnu.wardrobe.outfits.constraints import Constraints
from kyapehnu.wardrobe.outfits.repetition import History
from kyapehnu.wardrobe.outfits.scoring import ScoreContext

TODAY = date(2026, 10, 6)


def item(item_id: int, category: str = "top", **fields: Any) -> Item:
    return Item(**{"id": item_id, "user_id": 1, "name": f"item {item_id}", "category": category, **fields})


def constraints(**fields: Any) -> Constraints:
    defaults: dict[str, Any] = {
        "warmth_min": 1,
        "warmth_max": 5,
        "layer": "optional",
        "marked_down": frozenset(),
        "rain_likely": False,
        "notes": (),
    }
    return Constraints(**{**defaults, **fields})


def make_ctx(**fields: Any) -> ScoreContext:
    defaults: dict[str, Any] = {
        "formality": (1, 5),
        "cons": None,
        "history": History(last_worn={}, outfit_last_worn={}, audience_wears=[]),
        "today": TODAY,
        "weights": Weights(),
        "repetition": RepetitionPrefs(),
    }
    return ScoreContext(**{**defaults, **fields})
