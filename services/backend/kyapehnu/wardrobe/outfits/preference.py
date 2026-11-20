"""What the user's thumbs taught (ADR-0014): a score for the tags an outfit's items carry.

Pure and I/O free except `load`. With no feedback every outfit scores the same neutral 0.5, so the order of
suggestions is the one without a preference.
"""

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func
from sqlmodel import Session, col, select

from kyapehnu.wardrobe.models import Feedback, Item, Outfit

TANH_SCALE: Final = 4.0  # ponytail: hand-tuned divisor; four net thumbs on a tag take the part about 0.38 from neutral
NEUTRAL_PART: Final = 0.5


def item_tags(i: Item) -> frozenset[str]:
    """Colour names, pattern, fabric, category, subcategory and the user's tags, trimmed and in lower case."""
    raw = (*(c.get("name", "") for c in i.colors), i.pattern, i.fabric or "", i.category, i.subcategory, *i.tags)
    return frozenset(tag for text in raw if (tag := text.strip().casefold()))


@dataclass(frozen=True)
class TagPreference:
    net: Mapping[str, float]  # thumbs up minus thumbs down, per tag

    def part(self, items: Sequence[Item]) -> float:
        """0..1: neutral when no tag of the items has a net value. A tag two items share counts once."""
        if not self.net:
            return NEUTRAL_PART
        tags = frozenset[str]().union(*(item_tags(i) for i in items))
        return NEUTRAL_PART + NEUTRAL_PART * math.tanh(sum(self.net.get(t, 0.0) for t in tags) / TANH_SCALE)


NEUTRAL: Final = TagPreference({})


def load(user_id: int, s: Session, items_by_id: Mapping[int, Item]) -> TagPreference:
    """What the latest thumb of every rated outfit says about the tags of its items (AC-20).

    `items_by_id` holds the items that exist now: a deleted item is skipped and the tags it has now are the ones that
    count (AC-53). One query, however many ratings there are.
    """
    latest = select(func.max(col(Feedback.id))).where(Feedback.user_id == user_id).group_by(col(Feedback.outfit_id))
    rated = (
        select(col(Feedback.rating), col(Outfit.item_ids))
        .join(Outfit)
        .where(Outfit.user_id == user_id, col(Feedback.id).in_(latest))
    )
    net: defaultdict[str, float] = defaultdict(float)
    for rating, item_ids in s.exec(rated):
        for tag in frozenset[str]().union(*(item_tags(items_by_id[i]) for i in item_ids if i in items_by_id)):
            net[tag] += rating
    return TagPreference(dict(net)) if net else NEUTRAL
