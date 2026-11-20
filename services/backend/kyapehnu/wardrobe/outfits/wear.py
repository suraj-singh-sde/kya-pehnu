"""Wear log and saved outfits. The only writer of the outfit and wearlog tables.

An outfit is one saved set of items (unique by `item_key`). Item ids are kept as JSON with no foreign key, so a deleted
item leaves its outfits and wears in place: they list only the items that still exist and are marked incomplete.
"""

import logging
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from datetime import date, timedelta
from typing import cast

from sqlalchemy import ColumnElement, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped
from sqlmodel import Session, col, select

from kyapehnu.core.config import RepetitionPrefs
from kyapehnu.core.context import Ctx
from kyapehnu.core.db import get_owned
from kyapehnu.core.errors import NotFoundError, PlaceNotFoundError
from kyapehnu.core.limits import MAX_WEARS
from kyapehnu.files import public as files
from kyapehnu.wardrobe import events
from kyapehnu.wardrobe.events import LIST_LIMIT
from kyapehnu.wardrobe.models import Event, Feedback, Item, Outfit, WearLog
from kyapehnu.wardrobe.outfits import collage
from kyapehnu.wardrobe.outfits.repetition import History
from kyapehnu.wardrobe.outfits.schemas import OutfitOut, Thumb, WearIn, WearLogOut, WearsQuery
from kyapehnu.wardrobe.outfits.slots import SLOT_ORDER, SlottedItem, slot_of
from kyapehnu.wardrobe.owned import find_items, require_owned_items
from kyapehnu.weather.summary import for_event

log = logging.getLogger(__name__)


def item_key(ids: Iterable[int]) -> str:
    """The identity of a set of items: the sorted ids, so the order they were worn in does not matter."""
    return ",".join(str(i) for i in sorted(ids))


def _id(row: Item | Outfit) -> int:
    return cast(int, row.id)  # saved rows always have one


def _holds(column: Mapped[list[int]], item: int) -> ColumnElement[bool]:
    """SQL test: the JSON list in `column` contains the item id."""
    each = func.json_each(column).table_valued("value")
    return select(each.c.value).where(each.c.value == item).exists()


def _existing_ids(user_id: int, s: Session, ids: Collection[int]) -> set[int]:
    if not ids:
        return set()
    stmt = select(col(Item.id)).where(Item.user_id == user_id, col(Item.id).in_(ids))
    return cast(set[int], set(s.exec(stmt)))  # saved rows have an id


def _wear_out(w: WearLog, existing: set[int]) -> WearLogOut:
    return WearLogOut(
        id=cast(int, w.id),
        outfit_id=w.outfit_id,
        item_ids=[i for i in w.item_ids if i in existing],
        worn_on=w.worn_on,
        event_id=w.event_id,
        audience_tags=w.audience_tags,
    )


def _wear_outs(user_id: int, s: Session, wears: Sequence[WearLog]) -> list[WearLogOut]:
    existing = _existing_ids(user_id, s, {i for w in wears for i in w.item_ids})
    return [_wear_out(w, existing) for w in wears]


def _audiences(wears_newest_first: Sequence[WearLog]) -> list[str]:
    """Everyone the outfit was worn for, the spelling of the most recent wear kept."""
    spelling: dict[str, str] = {}
    for w in wears_newest_first:
        for name in w.audience_tags:
            spelling.setdefault(name.casefold(), name)
    return list(spelling.values())


def _present(user_id: int, s: Session, outfits: Sequence[Outfit]) -> list[OutfitOut]:
    """The outfits as the API shows them: most recently worn first, never worn ones last (newest first)."""
    wears: dict[int, list[WearLog]] = defaultdict(list)
    if outfits:
        stmt = (
            select(WearLog)
            .where(WearLog.user_id == user_id, col(WearLog.outfit_id).in_([_id(o) for o in outfits]))
            .order_by(col(WearLog.worn_on).desc(), col(WearLog.id).desc())
        )
        for w in s.exec(stmt):
            wears[cast(int, w.outfit_id)].append(w)

    def last_worn(o: Outfit) -> date:
        return wears[_id(o)][0].worn_on if wears[_id(o)] else date.min

    newest_first = sorted(outfits, key=lambda o: (o.created_at, _id(o)), reverse=True)
    ordered = sorted(newest_first, key=last_worn, reverse=True)  # a stable sort keeps "newest first" on ties
    stored_ids = {i for o in ordered for i in o.item_ids}
    found = find_items(user_id, s, stored_ids)
    ratings = latest_ratings(user_id, s, [_id(o) for o in ordered])
    return [_outfit_out(o, wears[_id(o)], found, ratings.get(_id(o))) for o in ordered]


def _outfit_out(
    o: Outfit, wears_newest_first: Sequence[WearLog], found: dict[int, Item], rating: int | None
) -> OutfitOut:
    present = [i for i in o.item_ids if i in found]
    return OutfitOut(
        id=cast(int, o.id),
        name=o.name,
        item_ids=present,
        items=[found[i] for i in present],
        incomplete=len(present) != len(o.item_ids),
        collage_path=o.collage_path,
        reason=o.reason,
        occasion_tags=o.occasion_tags,
        created_at=o.created_at,
        times_worn=len(wears_newest_first),
        last_worn=wears_newest_first[0].worn_on if wears_newest_first else None,
        audiences=_audiences(wears_newest_first),
        rating=cast(Thumb | None, rating),  # the CHECK on feedback.rating keeps it to -1 and 1
    )


def load_slotted(user_id: int, s: Session, ids: Sequence[int]) -> list[SlottedItem]:
    """The items in slot order (a tie keeps the order given). An id that is missing or another user's is
    NotFoundError."""
    found = require_owned_items(user_id, s, ids)
    slotted = [(slot_of(found[i]), found[i]) for i in ids]
    return sorted(slotted, key=lambda pair: SLOT_ORDER.index(pair[0]))


def _weather_snapshot(ctx: Ctx, event: Event | None, today: date) -> dict[str, object] | None:
    """The weather at the event for the record. A wear is never refused for lack of weather."""
    if event is None:
        return None
    try:
        summary = for_event(ctx.weather, event.place, event.starts_at, event.ends_at, today, ctx.prefs.weather)
    except PlaceNotFoundError:
        return None
    return summary.model_dump(mode="json") if summary else None


def _by_key(user_id: int, s: Session, key: str) -> Outfit | None:
    return s.exec(select(Outfit).where(Outfit.user_id == user_id, Outfit.item_key == key)).first()


def discard_collage(store: files.FileStore, key: str | None) -> None:
    """Delete a collage no row will point to. A failure is logged: the orphan sweep removes the file later."""
    files.discard(store, [key])


def _attach(s: Session, outfit: Outfit, log_row: WearLog, drawn: str | None) -> None:
    """Add the wear to the outfit, which takes the new collage when it has none.

    The flush takes the write lock, so the check after it sees every hard delete that committed before: a collage must
    never show an item that is gone (FR-27).
    """
    if outfit.collage_path is None:
        outfit.collage_path = drawn
    s.add(outfit)
    s.flush()
    if missing := set(log_row.item_ids) - _existing_ids(log_row.user_id, s, log_row.item_ids):
        s.rollback()
        raise NotFoundError(f"Item {min(missing)} not found")
    log_row.outfit_id = outfit.id
    s.add(log_row)


def _save(s: Session, outfit: Outfit, log_row: WearLog, drawn: str | None) -> Outfit:
    """Commit the wear. When another request saved the same set first (unique item_key), the wear joins its outfit."""
    key = outfit.item_key
    try:
        _attach(s, outfit, log_row, drawn)
        s.commit()
    except IntegrityError:
        s.rollback()
        winner = _by_key(outfit.user_id, s, key)
        if winner is None:  # not the item_key race
            raise
        outfit = winner
        _attach(s, outfit, log_row, drawn)
        s.commit()
    return outfit


def wear(user_id: int, ctx: Ctx, s: Session, body: WearIn, today: date) -> OutfitOut:
    """Log a worn set of items and show the outfit as the API does."""
    return _present(user_id, s, [record_wear(user_id, ctx, s, body, today)])[0]


def record_wear(user_id: int, ctx: Ctx, s: Session, body: WearIn, today: date) -> Outfit:
    """Log a worn set of items, saving it as an outfit (or reusing the saved one) with a collage.

    Nothing is written before the collage is drawn, so no database lock is held while the pictures are read. With a
    session that does not expire its rows nothing is read after the commit, so an error raised here means no wear was
    written.
    """
    slotted = load_slotted(user_id, s, body.item_ids)
    event = events.get(user_id, s, body.event_id) if body.event_id is not None else None
    ids = [_id(i) for _, i in slotted]
    worn_on = body.worn_on or today
    snapshot = _weather_snapshot(ctx, event, today)
    existing = _by_key(user_id, s, item_key(ids))
    drawn = collage.render(ctx.files, user_id, slotted) if existing is None or existing.collage_path is None else None
    outfit = existing or Outfit(
        user_id=user_id,
        item_ids=ids,
        item_key=item_key(ids),
        name=f"{event.type.capitalize() if event else 'Outfit'} · {worn_on.day} {worn_on:%b}",
        occasion_tags=[event.type] if event else [],
        reason=body.reason,
    )
    log_row = WearLog(
        user_id=user_id,
        item_ids=ids,
        worn_on=worn_on,
        event_id=event.id if event else None,
        audience_tags=event.audience_tags if event else [],
        weather_snapshot=snapshot,
    )
    try:
        saved = _save(s, outfit, log_row, drawn)
    except Exception:
        discard_collage(ctx.files, drawn)
        raise
    if saved.collage_path != drawn:  # the winner of a race already had a collage
        discard_collage(ctx.files, drawn)
    log.info("event=wear_logged outfit_id=%s items=%d", saved.id, len(ids))
    return saved


def worn_today(user_id: int, s: Session, item_id: int, today: date) -> tuple[WearLogOut, bool]:
    """(the log, created): a second press on the same day returns the log of the first instead of a second row."""
    get_owned(user_id, s, Item, item_id)
    existing = s.exec(
        select(WearLog).where(WearLog.user_id == user_id, WearLog.worn_on == today, col(WearLog.item_ids) == [item_id])
    ).first()
    if existing is not None:
        return _wear_outs(user_id, s, [existing])[0], False
    row = WearLog(user_id=user_id, item_ids=[item_id], worn_on=today)
    s.add(row)
    s.commit()
    s.refresh(row)
    return _wear_outs(user_id, s, [row])[0], True


def list_outfits(user_id: int, s: Session) -> list[OutfitOut]:
    # ponytail: orders every outfit in Python before slicing (O(all outfits)); move the sort into SQL if profiling asks
    return _present(user_id, s, list(s.exec(select(Outfit).where(Outfit.user_id == user_id)).all()))[:LIST_LIMIT]


def item_history(user_id: int, s: Session, item_id: int) -> tuple[list[WearLogOut], list[OutfitOut]]:
    get_owned(user_id, s, Item, item_id)
    wears = s.exec(
        select(WearLog)
        .where(WearLog.user_id == user_id, _holds(col(WearLog.item_ids), item_id))
        .order_by(col(WearLog.worn_on).desc(), col(WearLog.id).desc())
        .limit(LIST_LIMIT)
    ).all()
    outfits = s.exec(
        select(Outfit).where(Outfit.user_id == user_id, _holds(col(Outfit.item_ids), item_id)).limit(LIST_LIMIT)
    ).all()
    return _wear_outs(user_id, s, wears), _present(user_id, s, outfits)


def detach_item(user_id: int, s: Session, item_id: int) -> list[str]:
    """Clear the collage of every outfit that shows the item, which is about to go. The caller commits and deletes the
    returned files afterwards."""
    shown = s.exec(
        select(Outfit).where(
            Outfit.user_id == user_id, _holds(col(Outfit.item_ids), item_id), col(Outfit.collage_path).is_not(None)
        )
    ).all()
    rels = [cast(str, o.collage_path) for o in shown]
    for o in shown:
        o.collage_path = None
        s.add(o)
    return rels


def incomplete_keys(user_id: int, s: Session, existing: set[int]) -> set[frozenset[int]]:
    """The remaining items of every outfit that lost one: such a set must never be suggested again."""
    return {
        frozenset(remaining)
        for ids in s.exec(select(col(Outfit.item_ids)).where(Outfit.user_id == user_id))
        if len(remaining := [i for i in ids if i in existing]) != len(ids)
    }


def latest_ratings(user_id: int, s: Session, outfit_ids: Collection[int]) -> dict[int, int]:
    """The latest thumb of each rated outfit among `outfit_ids` (an outfit never rated is left out)."""
    if not outfit_ids:
        return {}
    latest = (
        select(func.max(col(Feedback.id)))
        .where(Feedback.user_id == user_id, col(Feedback.outfit_id).in_(outfit_ids))
        .group_by(col(Feedback.outfit_id))
    )
    rows = s.exec(select(col(Feedback.outfit_id), col(Feedback.rating)).where(col(Feedback.id).in_(latest)))
    return {outfit_id: rating for outfit_id, rating in rows}


def ratings_by_key(user_id: int, s: Session, keys: Collection[str]) -> dict[str, int]:
    """The latest thumb of each rated saved outfit, by its item_key."""
    if not keys:
        return {}
    owned = select(Outfit).where(Outfit.user_id == user_id, col(Outfit.item_key).in_(keys))
    key_of = {_id(row): row.item_key for row in s.exec(owned)}
    return {key_of[outfit_id]: rating for outfit_id, rating in latest_ratings(user_id, s, key_of.keys()).items()}


def save_outfit(user_id: int, ctx: Ctx, s: Session, ids: Sequence[int]) -> tuple[Outfit, str | None]:
    """The saved outfit of these items (saved now when new) and the collage drawn for it, None when it was not drawn.

    The outfit is flushed, not committed: the caller commits it with whatever refers to it, and deletes the drawn file
    when that commit fails. No wear is logged. A concurrent save of the same set wins (unique item_key): its outfit is
    returned and our collage is dropped.
    """
    slotted = load_slotted(user_id, s, ids)
    sorted_ids = [_id(i) for _, i in slotted]
    key = item_key(sorted_ids)
    if (saved := _by_key(user_id, s, key)) is not None:
        return saved, None
    drawn = collage.render(ctx.files, user_id, slotted)
    today = date.today()
    outfit = Outfit(
        user_id=user_id, item_ids=sorted_ids, item_key=key, name=f"Outfit · {today.day} {today:%b}", collage_path=drawn
    )
    try:
        s.add(outfit)
        s.flush()
        # The flush took the write lock: see `_attach`.
        if missing := set(sorted_ids) - _existing_ids(user_id, s, sorted_ids):
            raise NotFoundError(f"Item {min(missing)} not found")
    except IntegrityError:
        s.rollback()
        discard_collage(ctx.files, drawn)
        if (winner := _by_key(user_id, s, key)) is None:  # not the item_key race
            raise
        return winner, None
    except Exception:
        s.rollback()
        discard_collage(ctx.files, drawn)
        raise
    return outfit, drawn


def wear_history(user_id: int, s: Session, q: WearsQuery, today: date) -> list[WearLogOut]:
    """The wears of one item (`item_id`) or of one saved outfit (`item_ids`, any order) in the last `days` days, newest
    first, at most MAX_WEARS. A set that was never saved has no wears."""
    if q.item_id is not None:
        get_owned(user_id, s, Item, q.item_id)
        on_subject = _holds(col(WearLog.item_ids), q.item_id)
    else:
        ids = cast(list[int], q.item_ids)  # the schema guarantees one of the two targets
        require_owned_items(user_id, s, ids)
        saved = _by_key(user_id, s, item_key(ids))
        if saved is None:
            return []
        on_subject = col(WearLog.outfit_id) == saved.id
    since = today - timedelta(days=q.days)
    stmt = (
        select(WearLog)
        .where(WearLog.user_id == user_id, on_subject, col(WearLog.worn_on) >= since)
        .order_by(col(WearLog.worn_on).desc(), col(WearLog.id).desc())
        .limit(MAX_WEARS)
    )
    return _wear_outs(user_id, s, s.exec(stmt).all())


def _later[K](latest: dict[K, date], key: K, worn_on: date) -> None:
    latest[key] = max(worn_on, latest.get(key, worn_on))


def last_worn_by_item(user_id: int, s: Session) -> dict[int, date]:
    """The latest wear of every item that was worn, item-only wears included."""
    last: dict[int, date] = {}
    for ids, worn_on in s.exec(select(col(WearLog.item_ids), col(WearLog.worn_on)).where(WearLog.user_id == user_id)):
        for i in ids:
            _later(last, i, worn_on)
    return last


def load_history(user_id: int, s: Session, audience: Sequence[str], today: date, prefs: RepetitionPrefs) -> History:
    names = {name.strip().casefold() for name in audience}
    last_worn: dict[int, date] = {}
    outfit_last_worn: dict[frozenset[int], date] = {}
    audience_wears: list[tuple[frozenset[int], date]] = []
    rows = s.exec(
        select(col(WearLog.item_ids), col(WearLog.worn_on), col(WearLog.outfit_id), col(WearLog.audience_tags))
        .where(WearLog.user_id == user_id)
        .order_by(col(WearLog.id))
    )
    for ids, worn_on, outfit_id, tags in rows:
        for i in ids:
            _later(last_worn, i, worn_on)
        if outfit_id is None:  # an item-only wear says nothing about the outfit (AC-28)
            continue
        key = frozenset(ids)
        _later(outfit_last_worn, key, worn_on)
        if (today - worn_on).days <= prefs.audience_window_days and names & {t.strip().casefold() for t in tags}:
            audience_wears.append((key, worn_on))
    return History(last_worn=last_worn, outfit_last_worn=outfit_last_worn, audience_wears=audience_wears)
