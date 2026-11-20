"""Liveness and readiness. `/livez` says the process runs; `/readyz` says SQLite, Valkey and the pictures answer."""

import logging

import redis
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from kyapehnu.core.deps import CtxDep, KvDep
from kyapehnu.files.public import StoreUnavailableError

log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/livez", include_in_schema=False)
def livez() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readyz(ctx: CtxDep, kv: KvDep) -> JSONResponse:
    failed: list[str] = []
    try:
        with ctx.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        failed.append("db")
    try:
        kv.ping()
    except redis.RedisError:
        failed.append("valkey")
    try:
        ctx.files.ping()
    except StoreUnavailableError:
        failed.append("files")
    if failed:
        log.warning("event=not_ready failed=%s", ",".join(failed))
        return JSONResponse({"status": "unavailable", "failed": failed}, status_code=503)
    return JSONResponse({"status": "ok"})
