"""A weather summary for a place and a time, for the weather route and the chat's weather tool."""

from datetime import datetime
from typing import Annotated, Final, Self

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlmodel import Session

from kyapehnu.core.context import Ctx
from kyapehnu.core.schemas import Line
from kyapehnu.user.public import get_profile
from kyapehnu.wardrobe.events import check_local_time, check_window
from kyapehnu.wardrobe.outfits.constraints import derive
from kyapehnu.wardrobe.outfits.schemas import WeatherOut
from kyapehnu.weather.summary import for_event

HOME_LABEL: Final = "your home city"


def same_place(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def place_label(place: str, home_city: str) -> str:
    return HOME_LABEL if same_place(place, home_city) else place


class WeatherQuery(BaseModel):
    place: Annotated[Line, Field(max_length=120)] | None = None  # None or "" is the home city
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    outdoors: bool = False

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _local_time(cls, v: datetime | None) -> datetime | None:
        return check_local_time(v)

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.starts_at is not None:
            check_window(self.starts_at, self.ends_at)
        return self


class WeatherReport(BaseModel):
    place: str
    starts_at: datetime
    ends_at: datetime | None
    weather: WeatherOut | None


def report(user_id: int, ctx: Ctx, s: Session, q: WeatherQuery, now: datetime) -> WeatherReport:
    """The weather at a place and time (default: the home city, now), as the event weather is worked out.

    The place is shown as "your home city" when none was asked for or it is the home city.
    """
    home_city = get_profile(user_id, s).home_city
    s.rollback()  # end the read transaction: the weather calls below are slow
    starts_at = q.starts_at or now
    check_window(starts_at, q.ends_at)  # an end given without a start runs from now
    place = q.place or home_city
    found = for_event(ctx.weather, place, starts_at, q.ends_at, now.date(), ctx.prefs.weather)
    weather = None
    if found is not None:
        notes = derive(found, q.outdoors, ctx.prefs.weather).notes
        weather = WeatherOut(summary=found, notes=list(notes))
    return WeatherReport(place=place_label(place, home_city), starts_at=starts_at, ends_at=q.ends_at, weather=weather)
