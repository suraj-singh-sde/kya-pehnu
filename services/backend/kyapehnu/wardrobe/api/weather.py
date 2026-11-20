from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from kyapehnu.auth.public import CurrentUser
from kyapehnu.core import deps
from kyapehnu.core.deps import CtxDep, SessionDep
from kyapehnu.wardrobe import weather_report
from kyapehnu.wardrobe.weather_report import WeatherQuery, WeatherReport
from kyapehnu.weather.openmeteo import WeatherFetchError

router = APIRouter(prefix="/api/weather", tags=["weather"])


class PlacesOut(BaseModel):
    places: list[str]


@router.get("/places")
def suggest_places(q: Annotated[str, Query(min_length=1, max_length=120)], ctx: CtxDep) -> PlacesOut:
    """Place names near a spelling the geocoder did not know, for a "did you mean" list."""
    if ctx.weather is None:
        return PlacesOut(places=[])
    try:
        return PlacesOut(places=ctx.weather.suggest(q, ctx.weather.budget()))
    except WeatherFetchError:
        return PlacesOut(places=[])  # offline or slow: no suggestions, the typed name still gets refused elsewhere


@router.get("")
def get_weather(q: Annotated[WeatherQuery, Query()], s: SessionDep, ctx: CtxDep, user: CurrentUser) -> WeatherReport:
    return weather_report.report(user.id, ctx, s, q, deps.local_now())  # the clock the chat tool uses too
