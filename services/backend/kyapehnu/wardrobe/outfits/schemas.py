"""Request and response models of the outfit routes: suggest, swap, wear and the saved outfits."""

from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from kyapehnu.core.errors import InvalidInputError
from kyapehnu.core.limits import DEFAULT_DAYS, MAX_DAYS, MAX_EXCLUDE, MAX_INCLUDE, MAX_REASON_CHARS, MIN_DAYS
from kyapehnu.core.schemas import DistinctIds, ItemIds, Para, RowId
from kyapehnu.wardrobe.events import EventIn
from kyapehnu.wardrobe.models import Event, Item
from kyapehnu.wardrobe.outfits.slots import Slot
from kyapehnu.weather.summary import WeatherSummary

MAX_EXCLUDED_ITEMS = 100
EARLIEST_WORN_ON = date(2000, 1, 1)  # keeps a typo out of the history and clear of the date.min "never worn" mark


Thumb = Literal[-1, 1]  # a thumb down or up


class Parts(BaseModel):
    """The seven score parts of an outfit, each 0..1."""

    colour: float
    pattern: float
    formality: float
    weather: float
    repetition: float
    forgotten: float
    preference: float


class Problem(BaseModel):
    """Why no outfit could be built: which slot is the cause and why."""

    slot: Slot | None
    cause: Literal["missing", "filtered", "exhausted", "must_include"]
    message: str


class SuggestIn(BaseModel):
    event: EventIn | None = None
    event_id: RowId | None = None
    exclude: list[ItemIds] = Field(default=[], max_length=MAX_EXCLUDE)
    include: Annotated[DistinctIds, Field(max_length=MAX_INCLUDE)] = []  # items every outfit must contain

    @model_validator(mode="after")
    def _one_event_source(self) -> Self:
        if (self.event is None) == (self.event_id is None):
            raise InvalidInputError("send exactly one of event and event_id")
        return self


class SwapIn(BaseModel):
    event_id: RowId
    item_ids: ItemIds
    item_id: RowId  # the item to replace; the service checks it is one of item_ids
    exclude_item_ids: list[RowId] = Field(default=[], max_length=MAX_EXCLUDED_ITEMS)


class WearIn(BaseModel):
    item_ids: ItemIds
    event_id: RowId | None = None
    worn_on: date | None = None
    reason: Annotated[Para, Field(max_length=1000)] | None = None

    @field_validator("worn_on")
    @classmethod
    def _not_in_the_future(cls, v: date | None) -> date | None:
        if v is not None and v > date.today():
            raise InvalidInputError("worn_on cannot be in the future")
        if v is not None and v < EARLIEST_WORN_ON:
            raise InvalidInputError(f"worn_on cannot be before {EARLIEST_WORN_ON}")
        return v


class SwapHint(BaseModel):
    slot: Slot
    item_id: int
    item: Item


class SuggestionOut(BaseModel):
    item_ids: list[int]
    items: list[Item]
    score: float
    parts: Parts
    reason: str
    swap: SwapHint | None = None
    notes: list[str]
    rating: Thumb | None = None


class WeatherOut(BaseModel):
    summary: WeatherSummary
    notes: list[str]


class SuggestOut(BaseModel):
    event: Event
    weather: WeatherOut | None
    suggestions: list[SuggestionOut]
    problem: Problem | None = None


class MannequinIn(BaseModel):
    item_ids: ItemIds


class MannequinOut(BaseModel):
    path: str


class RefineIn(BaseModel):
    """One change to a mannequin picture of yours, in plain words."""

    path: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1, max_length=300)


class WearLogOut(BaseModel):
    id: int
    outfit_id: int | None
    item_ids: list[int]
    worn_on: date
    event_id: int | None
    audience_tags: list[str]


class OutfitOut(BaseModel):
    id: int
    name: str
    item_ids: list[int]
    items: list[Item]
    incomplete: bool
    collage_path: str | None
    reason: str | None
    occasion_tags: list[str]
    created_at: datetime
    times_worn: int
    last_worn: date | None
    audiences: list[str]
    rating: Thumb | None = None


class FeedbackIn(BaseModel):
    outfit_id: RowId | None = None
    item_ids: ItemIds | None = None
    rating: Thumb
    reason: Annotated[Para, Field(max_length=MAX_REASON_CHARS)] = ""

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        if (self.outfit_id is None) == (self.item_ids is None):
            raise InvalidInputError("send exactly one of outfit_id and item_ids")
        return self


class FeedbackOut(BaseModel):
    id: int
    outfit_id: int
    rating: int
    reason: str
    created_at: datetime


class WearsQuery(BaseModel):
    item_id: RowId | None = None
    item_ids: ItemIds | None = None
    days: int = Field(default=DEFAULT_DAYS, ge=MIN_DAYS, le=MAX_DAYS)

    @model_validator(mode="after")
    def _one_subject(self) -> Self:
        if (self.item_id is None) == (self.item_ids is None):
            raise InvalidInputError("send exactly one of item_id and item_ids")
        return self
