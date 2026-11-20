from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Column, Index
from sqlmodel import Field, SQLModel

from kyapehnu.core.db import utcnow


class ChatRole(StrEnum):
    user = "user"
    assistant = "assistant"
    notice = "notice"


class ProposalState(StrEnum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    expired = "expired"


class LlmCall(SQLModel, table=True):
    __table_args__ = (Index("ix_llmcall_user_id_id", "user_id", "id"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    provider: str
    model: str
    purpose: str = Field(index=True)
    request_summary: str
    response: str | None = None
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)


class ChatSession(SQLModel, table=True):
    # AUTOINCREMENT: a deleted chat's id is never handed out again, so an old `session=<id>` Debug row or a /chat/<id>
    # link can never reach a newer chat.
    __table_args__ = (
        Index("ix_chatsession_user_id_updated_at", "user_id", "updated_at"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    title: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow, index=True)


class ChatMessage(SQLModel, table=True):
    __table_args__ = (
        Index("ix_chatmessage_session_id_id", "session_id", "id"),
        Index("ix_chatmessage_user_id", "user_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    session_id: int = Field(foreign_key="chatsession.id")
    turn: int
    step: int  # 0 is the user's message
    role: str  # ChatRole
    content: str
    created_at: datetime = Field(default_factory=utcnow)


class ToolCall(SQLModel, table=True):
    __table_args__ = (
        CheckConstraint(
            f"proposal_state IN ({', '.join(repr(state.value) for state in ProposalState)})",
            name="ck_toolcall_proposal_state",
        ),
        Index("ix_toolcall_session_id_id", "session_id", "id"),
        Index("ix_toolcall_user_id_id", "user_id", "id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    session_id: int = Field(foreign_key="chatsession.id")
    turn: int
    step: int
    call_id: str  # the provider's id, echoed back in the history
    name: str
    args: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    result: str  # exactly what the model got
    truncated: bool = False
    error: str | None = None  # the tool error code
    latency_ms: int
    event_id: int | None = Field(default=None, foreign_key="event.id", index=True)
    ui: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    proposal_state: str | None = None  # ProposalState
    proposed_on: date | None = None
    decided_at: datetime | None = None
    outcome: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
