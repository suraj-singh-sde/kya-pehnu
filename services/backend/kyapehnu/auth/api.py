"""The /api/auth routes: Google sign-in start and callback, who am I, sign out.

The callback is a browser navigation, so every sign-in failure is a redirect to /signin with a stable reason code, never
a JSON error."""

import logging
from typing import Final

import httpx
import httpx2
from authlib.integrations.base_client.errors import MismatchingStateError, OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from joserfc.errors import JoseError
from pydantic import BaseModel
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.auth.deps import SESSION_COOKIE, CurrentUser, OAuthDep, SessionsDep
from kyapehnu.auth.models import User
from kyapehnu.auth.oauth import HOME, OAUTH_COOKIE, OAUTH_COOKIE_PATH, safe_next
from kyapehnu.auth.ratelimit import auth_rate_limit
from kyapehnu.auth.sessions import SessionStore
from kyapehnu.core.config import Settings
from kyapehnu.core.context import Ctx
from kyapehnu.core.db import get_one, utcnow
from kyapehnu.core.deps import CtxDep, SessionDep, SettingsDep
from kyapehnu.core.errors import GoogleUnreachableError
from kyapehnu.user.public import ensure_profile

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

CALLBACK_PATH: Final = "/api/auth/google/callback"
SIGN_IN_PATH: Final = "/signin"
SESSION_COOKIE_MAX_AGE: Final = 400 * 24 * 3600  # a carrier only: the idle time kept in Valkey is the authority
MAX_SUB_LENGTH: Final = 255
NEXT_KEY: Final = "next"
# authlib 1.8 talks through httpx2, a separate package whose errors are not httpx errors.
NETWORK_ERRORS: Final = (httpx.HTTPError, httpx2.HTTPError)
# What authlib and joserfc raise for a bad or unusable token (see the callback).
TOKEN_ERRORS: Final = (OAuthError, JoseError, RuntimeError, ValueError, KeyError)

# The Google-side failure names the browser is sent back with (shown on the sign-in page).
STATE: Final = "state"
GOOGLE_ERROR: Final = "google_error"
GOOGLE_UNREACHABLE: Final = "google_unreachable"
TOKEN: Final = "token"
UNVERIFIED_EMAIL: Final = "unverified_email"


class MeOut(BaseModel):
    id: int
    display_name: str
    email: str
    is_admin: bool
    avatar_url: str | None


def _refused(reason: str, exc: BaseException | None = None) -> RedirectResponse:
    detail = "" if exc is None else f" exc={type(exc).__name__}"  # the class only: its text may carry a token
    log.info("event=signin_failed reason=%s%s", reason, detail)
    return RedirectResponse(f"{SIGN_IN_PATH}?error={reason}", status_code=302)


@router.get("/google/login", dependencies=[Depends(auth_rate_limit)])
async def login(request: Request, oauth: OAuthDep, settings: SettingsDep, next: str | None = None) -> Response:
    if oauth is None:
        return RedirectResponse(f"{SIGN_IN_PATH}?error=auth_not_configured", status_code=302)
    request.session[NEXT_KEY] = safe_next(next)
    try:
        redirect: Response = await oauth.google.authorize_redirect(request, f"{settings.public_origin}{CALLBACK_PATH}")
        return redirect
    except (*NETWORK_ERRORS, ValueError) as e:  # ValueError: the discovery document was not JSON
        request.session.clear()
        log.warning("event=signin_failed reason=%s exc=%s", GOOGLE_UNREACHABLE, type(e).__name__)
        raise GoogleUnreachableError from e


@router.get("/google/callback", dependencies=[Depends(auth_rate_limit)])
async def callback(
    request: Request, oauth: OAuthDep, ctx: CtxDep, sessions: SessionsDep, settings: SettingsDep
) -> Response:
    # The session middleware expires the state cookie when it held something; one it could not read (too old) is not
    # touched, so that case is expired here.
    unreadable = OAUTH_COOKIE in request.cookies and not request.session
    try:
        response = await _complete_sign_in(request, oauth, ctx, sessions, settings)
    finally:
        request.session.clear()  # the state is single use, on every way out
    if unreadable:
        response.delete_cookie(OAUTH_COOKIE, path=OAUTH_COOKIE_PATH, httponly=True, samesite="lax")
    return response


async def _complete_sign_in(
    request: Request, oauth: OAuth | None, ctx: Ctx, sessions: SessionStore, settings: Settings
) -> Response:
    if oauth is None:
        return _refused("auth_not_configured")
    if "error" in request.query_params:
        return _refused(GOOGLE_ERROR)
    if not request.query_params.get("state"):
        return _refused(STATE)  # before authlib, so Google is not asked
    try:
        token = await oauth.google.authorize_access_token(request)
    except MismatchingStateError as e:
        return _refused(STATE, e)
    except NETWORK_ERRORS as e:
        return _refused(GOOGLE_UNREACHABLE, e)
    except TOKEN_ERRORS as e:
        return _refused(TOKEN, e)
    info = token.get("userinfo")
    if not isinstance(info, dict):
        return _refused(TOKEN)
    sub, email = info.get("sub"), info.get("email")
    if not (isinstance(sub, str) and 0 < len(sub) <= MAX_SUB_LENGTH and isinstance(email, str) and email):
        return _refused(TOKEN)
    if info.get("email_verified") is not True:
        return _refused(UNVERIFIED_EMAIL)
    name = info.get("name")
    # Only the profile fields are kept: the Google tokens are dropped with `token` (NFR-1).
    user_id, cookie = await run_in_threadpool(
        _open_session,
        ctx,
        sessions,
        sub,
        email,
        name if isinstance(name, str) and name else email,
        request.cookies.get(SESSION_COOKIE),
        info.get("picture"),
    )
    log.info("event=signin_ok user_id=%s", user_id)
    response = RedirectResponse(safe_next(request.session.get(NEXT_KEY, HOME)), status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        cookie,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.public_https,
        path="/",
    )
    return response


def _open_session(
    ctx: Ctx, sessions: SessionStore, sub: str, email: str, name: str, previous: str | None, picture: object = None
) -> tuple[int, str]:
    """Sign the account in. The browser's earlier session ends here, so a replaced token cannot be used any more."""
    with Session(ctx.engine) as s:
        user = upsert_google_user(s, sub=sub, email=email, name=name, now=utcnow(), picture=picture)
        assert user.id is not None  # the row was just written and read back
        user_id = user.id
        ensure_profile(user_id, s, ctx.prefs)
    cookie = sessions.create(user_id)
    if previous:
        sessions.delete(previous)
    return user_id, cookie


class OptionsOut(BaseModel):
    google: bool  # Google sign-in is set up
    local: bool  # the email-only sign-in for a computer of one's own (KYAPEHNU_DEV_LOGIN, loopback only)


@router.get("/options")
def options(settings: SettingsDep) -> OptionsOut:
    return OptionsOut(google=settings.google_configured, local=settings.dev_login)


@router.get("/me")
def me(user: CurrentUser, s: SessionDep) -> MeOut:
    account = get_one(s, User, user.id)
    return MeOut(
        id=user.id,
        display_name=account.display_name,
        email=account.email,
        is_admin=user.is_admin,
        avatar_url=account.avatar_url,
    )


@router.post("/logout", status_code=204)
def logout(request: Request, user: CurrentUser, sessions: SessionsDep, settings: SettingsDep) -> Response:
    sessions.delete(request.cookies[SESSION_COOKIE])
    log.info("event=signout user_id=%s", user.id)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=settings.public_https)
    return response
