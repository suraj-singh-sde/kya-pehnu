"""`POST /api/auth/dev-login`: a session for any email, without Google. Development only.

Mounted only with `KYAPEHNU_DEV_LOGIN=1` on an instance whose public URL is a loopback address (compose.dev.yaml); the
app refuses to start with it anywhere else. The account goes through the same upsert as a Google sign-in, so the first
one is the admin."""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from kyapehnu.auth.api import SESSION_COOKIE_MAX_AGE, _open_session
from kyapehnu.auth.deps import SESSION_COOKIE, SessionsDep
from kyapehnu.auth.ratelimit import auth_rate_limit
from kyapehnu.core.deps import CtxDep, SettingsDep

router = APIRouter(prefix="/api/auth", tags=["auth"])


class DevLoginIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)


@router.post("/dev-login", status_code=204, dependencies=[Depends(auth_rate_limit)])
async def dev_login(
    body: DevLoginIn, request: Request, ctx: CtxDep, sessions: SessionsDep, settings: SettingsDep
) -> Response:
    email = body.email.lower()  # one account per address, whatever the case it was typed in
    _, cookie = await run_in_threadpool(
        _open_session, ctx, sessions, f"dev:{email}", email, email, request.cookies.get(SESSION_COOKIE)
    )
    response = Response(status_code=204)
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
