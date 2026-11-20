"""Scores an outfit from seven parts (colour, pattern, formality, weather, repetition, forgotten, preference)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from statistics import fmean, pstdev
from typing import Final

from kyapehnu.core.config import RepetitionPrefs, Weights
from kyapehnu.wardrobe.color import harmony
from kyapehnu.wardrobe.models import Item
from kyapehnu.wardrobe.outfits.constraints import Constraints, is_marked_down
from kyapehnu.wardrobe.outfits.preference import NEUTRAL, TagPreference
from kyapehnu.wardrobe.outfits.repetition import History, forgotten_part, is_forgotten, item_recency, repetition_part
from kyapehnu.wardrobe.outfits.schemas import Parts
from kyapehnu.wardrobe.outfits.slots import SlottedItem, item_id

NO_WEATHER_FIT: Final = 0.5
FORMALITY_SPREAD: Final = 2  # a distance (or standard deviation) of this many levels scores 0
WARMTH_SPREAD: Final = 4  # the largest possible warmth distance
MARKED_DOWN_FACTOR: Final = 0.5


@dataclass(frozen=True)
class ScoreContext:
    formality: tuple[int, int]
    cons: Constraints | None
    history: History
    today: date
    weights: Weights
    repetition: RepetitionPrefs
    preference: TagPreference = NEUTRAL


def pattern_score(items: Sequence[Item]) -> float:
    patterned = sum(i.pattern != "solid" for i in items)
    return 1.0 if patterned <= 1 else 0.5 ** (patterned - 1)


def _closeness(level: float, rng: tuple[int, int]) -> float:
    return max(0.0, 1 - abs(level - sum(rng) / 2) / FORMALITY_SPREAD)


def formality_fit(levels: Sequence[int], rng: tuple[int, int]) -> float:
    evenness = max(0.0, 1 - pstdev(levels) / FORMALITY_SPREAD)
    return 0.5 * _closeness(fmean(levels), rng) + 0.5 * evenness


def item_weather_fit(item: Item, cons: Constraints | None) -> float:
    if cons is None:
        return NO_WEATHER_FIT
    target = (cons.warmth_min + cons.warmth_max) / 2
    fit = max(0.0, 1 - abs(item.warmth - target) / WARMTH_SPREAD)
    return fit * MARKED_DOWN_FACTOR if is_marked_down(item, cons) else fit


def weather_fit(items: Sequence[Item], cons: Constraints | None) -> float:
    return NO_WEATHER_FIT if cons is None else fmean(item_weather_fit(i, cons) for i in items)


def weighted_total(parts: Parts, weights: Weights) -> float:
    return (
        weights.colour * parts.colour
        + weights.pattern * parts.pattern
        + weights.formality * parts.formality
        + weights.weather * parts.weather
        + weights.repetition * parts.repetition
        + weights.forgotten * parts.forgotten
        + weights.preference * parts.preference
    )


def item_prescore(item: Item, ctx: ScoreContext) -> float:
    """A cheap per-item score used to shortlist items and to cap the combinations (no colour, pattern or audience)."""
    last = ctx.history.last_worn.get(item_id(item))
    w = ctx.weights
    denominator = w.formality + w.weather + w.repetition + w.forgotten
    if denominator == 0:
        return 0.0
    numerator = (
        w.formality * _closeness(item.formality, ctx.formality)
        + w.weather * item_weather_fit(item, ctx.cons)
        + w.repetition * (1 - item_recency(last, ctx.today, ctx.repetition.half_life_days))
        + w.forgotten * is_forgotten(last, ctx.today, ctx.repetition.forgotten_days)
    )
    return numerator / denominator


def score(items: Sequence[SlottedItem], ctx: ScoreContext) -> tuple[float, Parts]:
    just_items = [i for _, i in items]
    parts = Parts(
        colour=harmony([i.colors[0] for i in just_items if i.colors]),
        pattern=pattern_score(just_items),
        formality=formality_fit([i.formality for i in just_items], ctx.formality),
        weather=weather_fit(just_items, ctx.cons),
        repetition=repetition_part(items, ctx.history, ctx.today, ctx.repetition),
        forgotten=forgotten_part(just_items, ctx.history.last_worn, ctx.today, ctx.repetition.forgotten_days),
        preference=ctx.preference.part(just_items),
    )
    return weighted_total(parts, ctx.weights), parts
