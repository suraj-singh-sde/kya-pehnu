"""Events a person plans for (type, time, place, who will be there). The only writer of the event table."""

import re
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Annotated, Final, Self, cast

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlmodel import Session, col, select

from kyapehnu.core.db import get_owned
from kyapehnu.core.errors import InvalidInputError
from kyapehnu.core.schemas import Line, Para
from kyapehnu.wardrobe.models import Event, EventType

LIST_LIMIT: Final = 200
MAX_EVENT_SPAN: Final = timedelta(days=7)
EARLIEST_EVENT: Final = datetime(2000, 1, 1)  # with the latest, keeps date arithmetic on a time far from the limits
LATEST_EVENT: Final = datetime(2100, 1, 1)
# A festival named in a draft's title or notes makes it a festival whatever type the chat chose: the model says "party"
# when the user says "Diwali party", and the festival's dress level is the point. Whole words, any letter case.
FESTIVALS: Final = re.compile(
    r"\b(?:diwali|deepavali|deepawali|holi|eid|ramzan|ramadan|navratri|navaratri|garba|dandiya|durga puja|dussehra|"
    r"dasara|christmas|xmas|pongal|onam|lohri|baisakhi|vaisakhi|ganesh chaturthi|ganpati|raksha bandhan|rakhi|"
    r"bhai dooj|karva chauth|karwa chauth|janmashtami|makar sankranti|sankranti|ugadi|gudi padwa|bihu|chhath)\b",
    re.IGNORECASE,
)


def check_local_time(v: datetime | None) -> datetime | None:
    """The rules for a time the user types: local (no offset) and within a range that date arithmetic handles."""
    if v is not None and v.tzinfo is not None:
        raise InvalidInputError("send local time without offset")
    if v is not None and not EARLIEST_EVENT <= v < LATEST_EVENT:
        raise InvalidInputError(f"event times must be in {EARLIEST_EVENT.year} to {LATEST_EVENT.year - 1}")
    return v


def check_window(starts_at: datetime, ends_at: datetime | None) -> None:
    if ends_at is not None and not timedelta(0) < ends_at - starts_at <= MAX_EVENT_SPAN:
        raise InvalidInputError("ends_at must be after starts_at and at most 7 days later")


class EventIn(BaseModel):
    type: EventType
    title: Annotated[Line, Field(max_length=120)] = ""
    starts_at: datetime  # a wall clock at the place, never an instant
    ends_at: datetime | None = None
    place: Annotated[Line, Field(min_length=1, max_length=120)]
    outdoors: bool = False
    dress_code: Annotated[Line, Field(max_length=120)] = ""
    audience_tags: list[Annotated[Line, Field(min_length=1, max_length=40)]] = Field(default=[], max_length=20)
    notes: Annotated[Para, Field(max_length=1000)] = ""

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _local_time(cls, v: datetime | None) -> datetime | None:
        return check_local_time(v)

    @field_validator("audience_tags")
    @classmethod
    def _distinct_names(cls, tags: list[str]) -> list[str]:
        spelling: dict[str, str] = {}
        for tag in tags:
            spelling.setdefault(tag.casefold(), tag)  # the first spelling wins
        return list(spelling.values())

    @model_validator(mode="after")
    def _window(self) -> Self:
        check_window(self.starts_at, self.ends_at)
        return self


class EventDraft(EventIn):
    """An event as the chat describes it: with no place it happens in the user's home city."""

    # EventIn requires a place; a draft may leave it out (None or "").
    place: Annotated[Line, Field(max_length=120)] | None = None  # type: ignore[assignment]


def create(user_id: int, s: Session, data: EventIn) -> Event:
    event = Event(user_id=user_id, **data.model_dump())
    s.add(event)
    s.commit()
    s.refresh(event)
    return event


def festival_named(*texts: str) -> bool:
    return any(FESTIVALS.search(text) for text in texts)


# What the chat sends as the place when the user named a setting, not a town; the geocoder knows no such place.
SETTING_WORDS: Final = frozenset(
    {
        "home",
        "at home",
        "my home",
        "house",
        "my house",
        "indoor",
        "indoors",
        "inside",
        "outdoor",
        "outdoors",
        "outside",
        "venue",
        "the venue",
        "city",
        "in city",
        "the city",
        "in the city",
        "my city",
        "home city",
        "my home city",
    }
)


def _place_or_home(place: str | None, home_city: str) -> str:
    return home_city if not place or " ".join(place.split()).casefold() in SETTING_WORDS else place


def from_draft(draft: EventDraft, home_city: str) -> EventIn:
    """The draft as a full event: with no place, or a setting word for one, it happens in the home city, and a
    festival named in its title or notes makes it a festival (a wedding stays a wedding)."""
    data = draft.model_dump()
    if draft.type != EventType.wedding and festival_named(draft.title, draft.notes):
        data["type"] = EventType.festival
    try:
        return EventIn.model_validate({**data, "place": _place_or_home(draft.place, home_city)})
    except ValidationError as e:  # only the home city is unchecked: the draft itself passed its own validation
        raise InvalidInputError("Give a place: the home city in the profile cannot be used") from e


def create_draft(user_id: int, s: Session, draft: EventDraft, home_city: str) -> Event:
    """Save a draft through the rules of the Outfits form; with no place the event is in the home city."""
    return create(user_id, s, from_draft(draft, home_city))


def save_draft(user_id: int, s: Session, draft: EventDraft, home_city: str) -> Event:
    """`create_draft` that reads nothing after the commit. With a session that does not expire its rows, an error
    raised here means nothing was saved, so a caller that retries on it cannot save the event twice."""
    event = Event(user_id=user_id, **from_draft(draft, home_city).model_dump())
    s.add(event)
    s.commit()
    return event


def get(user_id: int, s: Session, event_id: int) -> Event:
    return get_owned(user_id, s, Event, event_id)


def find_events(user_id: int, s: Session, ids: Collection[int]) -> dict[int, Event]:
    """The events of this user among `ids`; a missing one, or one of another user, is simply absent."""
    if not ids:
        return {}
    found = s.exec(select(Event).where(Event.user_id == user_id, col(Event.id).in_(ids)))
    return {cast(int, event.id): event for event in found}  # saved rows have an id


def list_events(user_id: int, s: Session) -> list[Event]:
    stmt = (
        select(Event)
        .where(Event.user_id == user_id)
        .order_by(col(Event.starts_at).desc(), col(Event.id).desc())
        .limit(LIST_LIMIT)
    )
    return list(s.exec(stmt).all())
