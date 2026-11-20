"""Suggest and swap: the event, its weather, the rules, and the AI pick put together.

Slow work never runs inside a write transaction (ADR-4): the weather is fetched before the event is saved, the event is
committed at once, and the session holds nothing while the provider is called.
"""

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final, cast

from sqlmodel import Session, select

from kyapehnu.agent.public import active_provider
from kyapehnu.core.context import Ctx
from kyapehnu.core.errors import ItemNotInOutfitError, NoAlternativeError
from kyapehnu.user.public import Profile, get_profile
from kyapehnu.wardrobe import events
from kyapehnu.wardrobe.events import EventIn
from kyapehnu.wardrobe.models import Event, Item, ItemStatus
from kyapehnu.wardrobe.outfits import judge, pick, preference, wear
from kyapehnu.wardrobe.outfits.candidates import Candidate, Pool, apply_include, build_pool, keeps_pair, rank
from kyapehnu.wardrobe.outfits.constraints import derive
from kyapehnu.wardrobe.outfits.schemas import (
    Problem,
    SuggestIn,
    SuggestionOut,
    SuggestOut,
    SwapHint,
    SwapIn,
    Thumb,
    WeatherOut,
)
from kyapehnu.wardrobe.outfits.scoring import ScoreContext, score
from kyapehnu.wardrobe.outfits.slots import SLOT_ORDER, Slot, item_id
from kyapehnu.wardrobe.outfits.view import ChatView
from kyapehnu.wardrobe.owned import require_owned_items
from kyapehnu.weather.summary import WeatherSummary, for_event

log = logging.getLogger(__name__)

NOTHING_NEW: Final = "No new outfits can be built for this event. Change the event or add items."
ANY_FORMALITY: Final = (1, 5)
CLOSEST: Final = (
    "Nothing in your wardrobe is quite right for this {type} (dress level or weather), so these are the closest "
    "looks you have."
)


@dataclass(frozen=True)
class _Plan:
    """Everything the rules need for one event, read in one go."""

    event: Event
    weather: WeatherOut | None
    profile: Profile
    pool: Pool
    problem: Problem | None
    scoring: ScoreContext
    incomplete: set[frozenset[int]]  # outfits that lost an item: never suggested as they are (AC-4)
    items_by_id: Mapping[int, Item]  # every item, whatever its status: the ids `include` may name
    closest: bool  # nothing met the dress level or weather: the pool holds the nearest looks instead


def _summary(ctx: Ctx, event: EventIn | Event, today: date) -> WeatherSummary | None:
    return for_event(ctx.weather, event.place, event.starts_at, event.ends_at, today, ctx.prefs.weather)


def _plan(user_id: int, ctx: Ctx, s: Session, event: Event, summary: WeatherSummary | None, today: date) -> _Plan:
    """Read everything the rules need. It leaves `s` empty (nothing attached, no transaction): later code must not
    rely on objects loaded through it."""
    prefs = ctx.prefs
    cons = derive(summary, event.outdoors, prefs.weather) if summary is not None else None
    every_item = list(s.exec(select(Item).where(Item.user_id == user_id)))
    wardrobe = [i for i in every_item if i.status != ItemStatus.retired]
    formality = getattr(prefs.outfits.formality, event.type)
    history = wear.load_history(user_id, s, event.audience_tags, today, prefs.repetition)
    incomplete = wear.incomplete_keys(user_id, s, {item_id(i) for i in every_item})
    profile = get_profile(user_id, s)
    items_by_id = {item_id(i): i for i in every_item}
    learned = preference.load(user_id, s, items_by_id)
    s.expunge_all()  # the rows stay readable; this ends the read transaction before any slow call
    s.rollback()
    pool, problem = build_pool(wardrobe, formality, cons, prefs.weather)
    closest = False
    if problem is not None and problem.cause == "filtered":
        # Nothing is quite right for the event: offer the closest looks rather than none, and say so. The scoring
        # still measures every look against the real dress level and weather, so the nearest come first.
        pool, problem = build_pool(wardrobe, ANY_FORMALITY, None, prefs.weather)
        closest = problem is None
    return _Plan(
        event=event,
        weather=WeatherOut(summary=summary, notes=list(cons.notes)) if summary is not None and cons else None,
        profile=profile,
        pool=pool,
        problem=problem,
        scoring=ScoreContext(formality, cons, history, today, prefs.outfits.weights, prefs.repetition, learned),
        incomplete=incomplete,
        items_by_id=items_by_id,
        closest=closest,
    )


def _suggestion(candidate: Candidate, reason: str, rating: int | None, swap: SwapHint | None = None) -> SuggestionOut:
    return SuggestionOut(
        item_ids=candidate.item_ids,
        items=[i for _, i in candidate.items],
        score=candidate.score,
        parts=candidate.parts,
        reason=reason,
        swap=swap,
        notes=list(candidate.notes),
        rating=cast(Thumb | None, rating),  # the CHECK on feedback.rating keeps it to -1 and 1
    )


def _picked_by(chosen: list[pick.Chosen]) -> str:
    by_ai = {c.by_ai for c in chosen}
    return "mixed" if len(by_ai) == 2 else "ai" if by_ai == {True} else "fallback"


def suggest(
    user_id: int, ctx: Ctx, s: Session, body: SuggestIn, today: date, view: ChatView | None = None
) -> SuggestOut:
    """Up to three outfits for an event, new or saved. `exclude` (regenerate) removes sets already shown."""
    started = time.perf_counter()
    require_owned_items(user_id, s, body.include)  # before an event is saved: a refused request writes nothing
    if body.event is not None:
        summary = _summary(ctx, body.event, today)  # an unknown place stops here, before anything is written (AC-45)
        event = events.create(user_id, s, body.event)
    else:
        event = events.get(user_id, s, cast(int, body.event_id))  # the schema guarantees one of the two
        summary = _summary(ctx, event, today)
    plan = _plan(user_id, ctx, s, event, summary, today)
    suggestions: list[SuggestionOut] = []
    pool, problem, required = plan.pool, plan.problem, frozenset[Slot]()
    if problem is None and body.include:
        pool, required, problem = apply_include(pool, body.include, plan.items_by_id)
    picked_by, judged = "none", False
    if problem is None:
        excluded = {frozenset(ids) for ids in body.exclude} | plan.incomplete
        prefs = ctx.prefs.outfits
        ranked = rank(pool, plan.scoring, excluded, prefs.per_slot, prefs.max_candidates, required)
        if not ranked:
            problem = Problem(slot=None, cause="exhausted", message=NOTHING_NEW)
        else:
            swappable = {item_id(i): (slot, i) for slot, items in pool.by_slot.items() for i in items}
            provider = active_provider(ctx)
            short = judge.shortlist(user_id, provider, ctx.files, ranked, pick.occasion(event))
            judged = short.judged
            chosen = pick.choose(
                user_id,
                provider,
                ranked,
                event,
                plan.weather,
                plan.profile,
                swappable,
                view,
                plan.closest,
                shown=short.candidates,
                pictures=short.pictures,
            )
            ratings = wear.ratings_by_key(user_id, s, {wear.item_key(c.candidate.item_ids) for c in chosen})
            suggestions = [
                _suggestion(c.candidate, c.reason, ratings.get(wear.item_key(c.candidate.item_ids)), c.swap)
                for c in chosen
            ]
            picked_by = _picked_by(chosen)
            if plan.closest:
                problem = Problem(slot=None, cause="filtered", message=CLOSEST.format(type=event.type))
    log.info(
        "event=outfit_suggest event_id=%s suggestions=%d picked_by=%s judged=%s weather=%s latency_ms=%d",
        event.id,
        len(suggestions),
        picked_by,
        judged,
        summary.source if summary else "none",
        (time.perf_counter() - started) * 1000,
    )
    return SuggestOut(event=event, weather=plan.weather, suggestions=suggestions, problem=problem)


def swap(user_id: int, ctx: Ctx, s: Session, body: SwapIn, today: date) -> SuggestionOut:
    """The outfit with one item replaced by the best other item of its slot. The rules decide: no AI call."""
    if body.item_id not in body.item_ids:
        raise ItemNotInOutfitError()
    event = events.get(user_id, s, body.event_id)
    current = wear.load_slotted(user_id, s, body.item_ids)
    plan = _plan(user_id, ctx, s, event, _summary(ctx, event, today), today)
    slot = next(slot for slot, i in current if item_id(i) == body.item_id)
    kept = [(sl, i) for sl, i in current if item_id(i) != body.item_id]
    held = frozenset(sl for sl, _ in kept)
    blocked = {*body.item_ids, *body.exclude_item_ids}
    best: tuple[Candidate, int] | None = None
    for spare in (i for i in plan.pool.by_slot[slot] if item_id(i) not in blocked and keeps_pair(i, slot, held)):
        items = sorted([*kept, (slot, spare)], key=lambda pair: SLOT_ORDER.index(pair[0]))
        if frozenset(item_id(i) for _, i in items) in plan.incomplete:
            continue
        total, parts = score(items, plan.scoring)
        # a tie goes to the lower id, so the answer does not depend on the order the items were read in
        if best is None or (total, -item_id(spare)) > (best[0].score, -best[1]):
            best = (Candidate(tuple(items), plan.pool.notes, total, parts), item_id(spare))
    if best is None:
        raise NoAlternativeError(f"No other {slot} fits this event")
    candidate = best[0]
    log.info("event=outfit_swap event_id=%s slot=%s", event.id, slot)
    rating = wear.ratings_by_key(user_id, s, [wear.item_key(candidate.item_ids)]).get(wear.item_key(candidate.item_ids))
    return _suggestion(candidate, pick.plain_reason(candidate.parts, event.type, plan.weather is not None), rating)
