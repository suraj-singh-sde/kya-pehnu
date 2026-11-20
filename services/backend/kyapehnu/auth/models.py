from datetime import datetime

from sqlalchemy import Index, false, text
from sqlmodel import Field, SQLModel

from kyapehnu.core.db import utcnow


class User(SQLModel, table=True):
    """One person, keyed by their Google account id. The first account to sign in is the admin."""

    # At most one admin, enforced by the database: two sign-ins racing for "first" cannot both win.
    __table_args__ = (Index("ux_user_one_admin", "is_admin", unique=True, sqlite_where=text("is_admin = 1")),)

    id: int | None = Field(default=None, primary_key=True)
    google_sub: str = Field(max_length=255, unique=True)
    email: str = Field(default="", max_length=320, index=True)
    display_name: str = Field(default="", max_length=200)
    avatar_url: str | None = Field(default=None, max_length=2048)  # Google's picture claim, https only
    is_admin: bool = Field(default=False, sa_column_kwargs={"server_default": false()})
    created_at: datetime = Field(default_factory=utcnow)
    last_sign_in_at: datetime = Field(default_factory=utcnow)
