"""What the other domains may use from the wardrobe (LLD 3.4).

Every function takes `user_id` first and sees that user's rows only: another user's id is NotFoundError.
"""

from collections.abc import Collection
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session

from kyapehnu.core.context import Ctx
from kyapehnu.core.db import get_owned
from kyapehnu.files.public import FileStore
from kyapehnu.wardrobe import events, items, pictures
from kyapehnu.wardrobe import seed as seeding
from kyapehnu.wardrobe import weather_report as weather
from kyapehnu.wardrobe.events import EventDraft
from kyapehnu.wardrobe.events import from_draft as event_from_draft
from kyapehnu.wardrobe.ingest import pipeline
from kyapehnu.wardrobe.ingest.jobs import Worker
from kyapehnu.wardrobe.items import WardrobeQuery
from kyapehnu.wardrobe.models import Event, IngestJob, Item, ItemStatus, Outfit
from kyapehnu.wardrobe.outfits import feedback, service
from kyapehnu.wardrobe.outfits import wear as wearing
from kyapehnu.wardrobe.outfits.pick import profile_line
from kyapehnu.wardrobe.outfits.schemas import (
    FeedbackIn,
    FeedbackOut,
    OutfitOut,
    Problem,
    SuggestIn,
    SuggestionOut,
    SuggestOut,
    SwapIn,
    Thumb,
    WearIn,
    WearLogOut,
    WearsQuery,
    WeatherOut,
)
from kyapehnu.wardrobe.outfits.view import ChatView
from kyapehnu.wardrobe.owned import find_items, require_owned_items
from kyapehnu.wardrobe.seed import SeedError
from kyapehnu.wardrobe.sweep import sweep_orphans
from kyapehnu.wardrobe.weather_report import HOME_LABEL, WeatherQuery, WeatherReport, place_label, same_place

__all__ = [
    "HOME_LABEL",
    "ChatView",
    "Event",
    "EventDraft",
    "FeedbackIn",
    "FeedbackOut",
    "Item",
    "ItemStatus",
    "OutfitOut",
    "Problem",
    "SeedError",
    "SuggestIn",
    "SuggestOut",
    "SuggestionOut",
    "SwapIn",
    "Thumb",
    "WardrobeQuery",
    "WearIn",
    "WearLogOut",
    "WearsQuery",
    "WeatherOut",
    "WeatherQuery",
    "WeatherReport",
    "Worker",
    "create_event",
    "event_from_draft",
    "create_folder_job",
    "find_events",
    "find_items",
    "get_event",
    "get_item",
    "get_job",
    "give_feedback",
    "item_history",
    "item_key",
    "list_events",
    "list_items",
    "list_wears",
    "place_label",
    "profile_line",
    "ratings_by_key",
    "require_owned_items",
    "run_job",
    "same_place",
    "retrim_thumbs",
    "seed",
    "set_status",
    "suggest",
    "swap",
    "sweep_orphans",
    "wear",
    "weather_report",
]

item_key = wearing.item_key
find_events = events.find_events


def list_items(
    user_id: int, s: Session, query: WardrobeQuery, *, brand: bool = True, today: date | None = None
) -> list[Item]:
    return items.list_items(user_id, s, **query.model_dump(), brand=brand, today=today)


def get_item(user_id: int, s: Session, item_id: int) -> Item:
    return get_owned(user_id, s, Item, item_id)


def set_status(user_id: int, s: Session, item_id: int, status: ItemStatus) -> None:
    """Commit a new status. Nothing is read back, so an error means nothing was written."""
    items.commit_patch(user_id, s, item_id, {"status": status})


def item_history(user_id: int, s: Session, item_id: int) -> tuple[list[WearLogOut], list[OutfitOut]]:
    return wearing.item_history(user_id, s, item_id)


def get_event(user_id: int, s: Session, event_id: int) -> Event:
    return events.get(user_id, s, event_id)


def list_events(user_id: int, s: Session) -> list[Event]:
    return events.list_events(user_id, s)


def create_event(user_id: int, s: Session, draft: EventDraft, home_city: str) -> Event:
    """Save a draft and read nothing after the commit (so a retry on an error cannot save it twice)."""
    return events.save_draft(user_id, s, draft, home_city)


def suggest(
    user_id: int, ctx: Ctx, s: Session, body: SuggestIn, today: date, view: ChatView | None = None
) -> SuggestOut:
    return service.suggest(user_id, ctx, s, body, today, view=view)


def swap(user_id: int, ctx: Ctx, s: Session, body: SwapIn, today: date) -> SuggestionOut:
    return service.swap(user_id, ctx, s, body, today)


def wear(user_id: int, ctx: Ctx, s: Session, body: WearIn, today: date) -> Outfit:
    """Log a worn set of items. With a session that does not expire its rows, an error means no wear was written."""
    return wearing.record_wear(user_id, ctx, s, body, today)


def list_wears(user_id: int, s: Session, q: WearsQuery, today: date) -> list[WearLogOut]:
    return wearing.wear_history(user_id, s, q, today)


def ratings_by_key(user_id: int, s: Session, keys: Collection[str]) -> dict[str, int]:
    return wearing.ratings_by_key(user_id, s, keys)


def give_feedback(user_id: int, ctx: Ctx, s: Session, body: FeedbackIn) -> FeedbackOut:
    return feedback.record(user_id, ctx, s, body)


def weather_report(user_id: int, ctx: Ctx, s: Session, q: WeatherQuery, now: datetime) -> WeatherReport:
    return weather.report(user_id, ctx, s, q, now)


def retrim_thumbs(engine: Engine, store: FileStore) -> int:
    return pictures.retrim_thumbs(engine, store)


def seed(user_id: int, engine: Engine, store: FileStore, force: bool = False) -> int:
    return seeding.seed(user_id, engine, store, force)


def get_job(user_id: int, s: Session, job_id: int) -> IngestJob:
    return get_owned(user_id, s, IngestJob, job_id)


def create_folder_job(user_id: int, s: Session, store: FileStore, folder: Path) -> IngestJob:
    return pipeline.create_folder_job(user_id, s, store, folder)


def run_job(user_id: int, ctx: Ctx, job_id: int) -> None:
    pipeline.run_job(user_id, ctx, job_id)
