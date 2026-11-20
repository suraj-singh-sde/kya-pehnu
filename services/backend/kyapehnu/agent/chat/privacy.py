"""What the model may be told (ADR-0012). Pure: no I/O. Every view is an allow-list of fields."""

from collections.abc import Mapping
from typing import Any, Final

from kyapehnu.wardrobe.public import (
    HOME_LABEL,
    ChatView,
    Event,
    Item,
    OutfitOut,
    WearLogOut,
    WeatherOut,
    place_label,
    same_place,
)

UNAVAILABLE: Final = "unavailable"  # what the model reads when there is no weather


def item_view(i: Item) -> dict[str, Any]:
    """No brand, price, notes, photo path or AI tags: only what picking an outfit needs."""
    return {
        "id": i.id,
        "name": i.name,
        "category": i.category,
        "subcategory": i.subcategory,
        "colours": [c["name"] for c in i.colors if c.get("name")],
        "pattern": i.pattern,
        "fabric": i.fabric,
        "formality": i.formality,
        "warmth": i.warmth,
        "seasons": list(i.seasons),
        "status": str(i.status),
        "tags": list(i.tags),
    }


def event_view(e: Event, v: ChatView) -> dict[str, Any]:
    """Type and times always. Title, notes and place only for an event this chat made; for any other event the place
    shows only as "your home city". The audience is never shown."""
    shown: dict[str, Any] = {
        "id": e.id,
        "type": str(e.type),
        "starts_at": e.starts_at.isoformat(),
        "ends_at": e.ends_at.isoformat() if e.ends_at else None,
        "outdoors": e.outdoors,
        "dress_code": e.dress_code,
    }
    if e.id in v.session_event_ids:
        return {**shown, "title": e.title, "notes": e.notes, "place": place_label(e.place, v.home_city)}
    if same_place(e.place, v.home_city):
        return {**shown, "place": HOME_LABEL}
    return shown


def weather_view(w: WeatherOut | None) -> dict[str, Any] | str:
    return {**w.summary.model_dump(mode="json"), "notes": w.notes} if w else UNAVAILABLE


def wear_view(w: WearLogOut, events: Mapping[int, Event], v: ChatView) -> dict[str, Any]:
    event = events.get(w.event_id) if w.event_id is not None else None
    return {
        "worn_on": w.worn_on.isoformat(),
        "item_ids": w.item_ids,
        "event": event_view(event, v) if event else None,
    }


def outfit_view(o: OutfitOut) -> dict[str, Any]:
    """No reason, collage or audiences."""
    return {"id": o.id, "item_ids": o.item_ids, "name": o.name}
