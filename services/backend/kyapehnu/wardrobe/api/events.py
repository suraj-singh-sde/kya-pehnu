from fastapi import APIRouter

from kyapehnu.auth.public import CurrentUser
from kyapehnu.core.deps import SessionDep
from kyapehnu.user.public import get_profile
from kyapehnu.wardrobe import events
from kyapehnu.wardrobe.events import EventDraft
from kyapehnu.wardrobe.models import Event

router = APIRouter(tags=["events"])


@router.get("/api/events")
def list_events(s: SessionDep, user: CurrentUser) -> list[Event]:
    return events.list_events(user.id, s)


@router.post("/api/events", status_code=201)
def create_event(body: EventDraft, s: SessionDep, user: CurrentUser) -> Event:
    return events.create_draft(user.id, s, body, get_profile(user.id, s).home_city)
