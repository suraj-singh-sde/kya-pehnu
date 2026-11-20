import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import Engine
from sqlmodel import Session, col, delete, select

from kyapehnu.agent.models import LlmCall

log = logging.getLogger(__name__)

LLM_CALL_RETENTION: Final = timedelta(days=30)


def db_logger(engine: Engine) -> Callable[[LlmCall], None]:
    def write(call: LlmCall) -> None:
        with Session(engine) as s:
            s.add(call)
            s.commit()

    return write


def recent_calls(
    user_id: int, s: Session, *, purpose: str | None, errors_only: bool, limit: int, session_id: int | None = None
) -> list[LlmCall]:
    """The caller's own calls, newest first."""
    stmt = select(LlmCall).where(col(LlmCall.user_id) == user_id).order_by(col(LlmCall.id).desc()).limit(limit)
    if session_id is not None:  # chat rows and the nested picks of a chat both start with this prefix
        stmt = stmt.where(col(LlmCall.request_summary).like(f"session={session_id} %"))
    if purpose:
        stmt = stmt.where(LlmCall.purpose == purpose)
    if errors_only:
        stmt = stmt.where(col(LlmCall.error).is_not(None))
    return list(s.exec(stmt).all())


def purge_llm_calls(engine: Engine, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(UTC)) - LLM_CALL_RETENTION
    with Session(engine) as s:
        count: int = s.exec(delete(LlmCall).where(col(LlmCall.created_at) < cutoff)).rowcount
        s.commit()
    log.info("event=llm_calls_purged count=%d", count)
    return count
