"""Who is calling: the session cookie decides."""

import logging
from dataclasses import dataclass
from typing import Annotated, Final, cast

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, Request
from sqlmodel import Session

from kyapehnu.auth.models import User
from kyapehnu.auth.sessions import SessionStore
from kyapehnu.core.deps import SessionDep, SettingsDep
from kyapehnu.core.errors import AuthNotConfiguredError, NotAuthenticatedError, PermissionDeniedError

log = logging.getLogger(__name__)

SESSION_COOKIE: Final = "kyapehnu_session"


@dataclass(frozen=True)
class Principal:
    id: int
    is_admin: bool


def get_sessions(request: Request) -> SessionStore:
    return cast(SessionStore, request.app.state.sessions)


SessionsDep = Annotated[SessionStore, Depends(get_sessions)]


def get_oauth(request: Request) -> OAuth | None:
    """The Google registry, or None when sign-in is not configured."""
    return cast(OAuth | None, request.app.state.oauth)


OAuthDep = Annotated[OAuth | None, Depends(get_oauth)]


def current_user(request: Request, sessions: SessionsDep, s: SessionDep, settings: SettingsDep) -> Principal:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise NotAuthenticatedError if settings.google_configured else AuthNotConfiguredError
    user_id = sessions.get_and_touch(cookie)
    if user_id is None:
        log.info("event=session_rejected")
        raise NotAuthenticatedError
    return _principal(s, user_id, sessions, cookie)


def _principal(s: Session, user_id: int, sessions: SessionStore, cookie: str) -> Principal:
    user = s.get(User, user_id)
    if user is None:  # the account is gone: end the session so the cookie stops costing a lookup
        sessions.delete(cookie)
        log.info("event=session_rejected")
        raise NotAuthenticatedError
    return Principal(user_id, user.is_admin)


CurrentUser = Annotated[Principal, Depends(current_user)]


def admin_user(user: CurrentUser) -> Principal:
    if not user.is_admin:
        log.info("event=admin_denied user_id=%s", user.id)
        raise PermissionDeniedError
    return user


AdminUser = Annotated[Principal, Depends(admin_user)]
