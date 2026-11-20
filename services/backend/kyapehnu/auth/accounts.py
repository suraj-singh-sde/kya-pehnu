"""Accounts: one per Google `sub`. Everything Google sends is untrusted text."""

import unicodedata
from datetime import datetime
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select, update

from kyapehnu.auth.models import User

MAX_EMAIL: Final = 320
MAX_NAME: Final = 200
MAX_PICTURE: Final = 2048
HIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf"})  # control and format (U+202E reverses the text that follows)


def _clean(value: str, limit: int) -> str:
    """No control or format characters (SQLite does not enforce `max_length`), no surrounding space, cut to `limit`."""
    return "".join(c for c in value if unicodedata.category(c) not in HIDDEN_CATEGORIES).strip()[:limit]


def _picture(value: object) -> str | None:
    """Google's `picture` claim, kept only as an https URL of sane length; anything else is no picture."""
    if not isinstance(value, str) or len(value) > MAX_PICTURE or not value.startswith("https://"):
        return None
    return value


def upsert_google_user(s: Session, *, sub: str, email: str, name: str, now: datetime, picture: object = None) -> User:
    """The account for this Google `sub`, created on first sign-in (the first account is the admin).

    The write lock is taken before the first read, so two first sign-ins cannot both see "no admin yet"."""
    email, name, avatar = _clean(email, MAX_EMAIL), _clean(name, MAX_NAME), _picture(picture)
    try:
        return _upsert(s, sub, email, name, avatar, now, first_may_be_admin=True)
    except IntegrityError:  # backstop: ux_user_one_admin refused a second admin
        s.rollback()
        return _upsert(s, sub, email, name, avatar, now, first_may_be_admin=False)


def _upsert(
    s: Session, sub: str, email: str, name: str, avatar: str | None, now: datetime, *, first_may_be_admin: bool
) -> User:
    s.connection().exec_driver_sql("BEGIN IMMEDIATE")
    updated = s.exec(
        update(User)
        .where(col(User.google_sub) == sub)
        .values(email=email, display_name=name, avatar_url=avatar, last_sign_in_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    if not updated:
        is_admin = first_may_be_admin and s.exec(select(User.id).where(col(User.is_admin))).first() is None
        s.add(
            User(
                google_sub=sub,
                email=email,
                display_name=name,
                avatar_url=avatar,
                is_admin=is_admin,
                last_sign_in_at=now,
            )
        )
    s.commit()
    return s.exec(select(User).where(col(User.google_sub) == sub)).one()


def user_id_for_email(s: Session, email: str) -> int:
    """The id of the one account with this email. The message never carries the email."""
    ids = s.exec(select(User.id).where(col(User.email) == email)).all()
    if len(ids) != 1 or ids[0] is None:
        raise LookupError("no single account matches that email")
    return ids[0]
