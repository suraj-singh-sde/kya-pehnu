"""Request and response models of the chat routes (LLD 3.2)."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from kyapehnu.agent.chat.limits import MAX_MESSAGE_CHARS
from kyapehnu.agent.models import ChatRole, ProposalState
from kyapehnu.core.schemas import Para
from kyapehnu.wardrobe.public import Event, Item, ItemStatus, Problem, Thumb, WeatherOut


class SessionSummary(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime


class MessageIn(BaseModel):
    text: Annotated[Para, Field(min_length=1, max_length=MAX_MESSAGE_CHARS)]  # cleaned, then 1 to 4000 characters


class MessageOut(BaseModel):
    id: int
    turn: int
    step: int
    role: ChatRole
    content: str
    created_at: datetime


class CardSuggestion(BaseModel):
    item_ids: list[int]  # as proposed, in slot order
    items: list[Item | None]  # aligned with item_ids; None is an item that was deleted
    reason: str
    notes: list[str]
    swap_item: Item | None
    rating: Thumb | None  # the latest thumb of the outfit with these items
    wearable: bool  # every item exists and is available


class OutfitsCard(BaseModel):
    event: Event | None
    weather: WeatherOut | None
    problem: Problem | None
    suggestions: list[CardSuggestion]


class ProposalView(BaseModel):
    tool_call_id: int
    kind: Literal["log_wear", "update_item_status"]
    state: ProposalState  # a pending proposal of an earlier day shows as expired
    proposed_on: date
    decided_at: datetime | None
    item_ids: list[int]  # log_wear
    items: list[Item | None]
    event: Event | None
    status_from: ItemStatus | None  # update_item_status
    status_to: ItemStatus | None
    actionable: bool  # still pending, same day, nothing it names has changed
    result: str | None


class ToolCallView(BaseModel):
    id: int
    turn: int
    step: int
    name: str
    error: str | None
    summary: str
    card: OutfitsCard | None
    event: Event | None
    proposal: ProposalView | None


class SessionOut(BaseModel):
    session: SessionSummary
    messages: list[MessageOut]
    tool_calls: list[ToolCallView]


class TurnOut(BaseModel):
    outcome: Literal["ok", "limit_reached", "provider_error", "no_tools"]
    turn: int
    session: SessionOut
