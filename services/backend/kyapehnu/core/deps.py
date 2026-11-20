from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, cast

import redis
from fastapi import Depends, Path, Request
from sqlmodel import Session

from kyapehnu.agent.turns import Turns
from kyapehnu.core.config import Settings
from kyapehnu.core.context import Ctx
from kyapehnu.core.schemas import MAX_ROW_ID
from kyapehnu.wardrobe.public import Worker


def local_now() -> datetime:
    """The machine's local time, naive. The one place the chat and weather routes read the clock (tests fix it)."""
    return datetime.now()


def get_ctx(request: Request) -> Ctx:
    return cast(Ctx, request.app.state.ctx)


def get_worker(request: Request) -> Worker:
    return cast(Worker, request.app.state.worker)


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_kv(request: Request) -> redis.Redis:
    return cast(redis.Redis, request.app.state.kv)


def get_turns(request: Request) -> Turns:
    return cast(Turns, request.app.state.turns)


def get_session(ctx: Annotated[Ctx, Depends(get_ctx)]) -> Iterator[Session]:
    with Session(ctx.engine) as s:
        yield s


CtxDep = Annotated[Ctx, Depends(get_ctx)]
WorkerDep = Annotated[Worker, Depends(get_worker)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
KvDep = Annotated[redis.Redis, Depends(get_kv)]
TurnsDep = Annotated[Turns, Depends(get_turns)]
SessionDep = Annotated[Session, Depends(get_session)]
PathId = Annotated[int, Path(le=MAX_ROW_ID)]
