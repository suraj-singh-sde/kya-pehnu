from typing import Annotated

from fastapi import APIRouter, Query

from kyapehnu.agent.chat import store
from kyapehnu.agent.llm import logging as llm_logging
from kyapehnu.agent.models import LlmCall, ToolCall
from kyapehnu.auth.public import AdminUser
from kyapehnu.core.deps import SessionDep
from kyapehnu.core.schemas import MAX_ROW_ID

# Raw prompts, replies and provider errors: every handler takes `AdminUser`, and shows only the admin's own rows.
router = APIRouter(prefix="/api/debug", tags=["debug"])

DEFAULT_CALL_LIMIT = 200
MAX_CALL_LIMIT = 1000

SessionFilter = Annotated[int | None, Query(ge=1, le=MAX_ROW_ID)]
CallLimit = Annotated[int, Query(ge=1, le=MAX_CALL_LIMIT)]


@router.get("/llm-calls")
def llm_calls(
    user: AdminUser,
    s: SessionDep,
    purpose: str | None = None,
    errors_only: bool = False,
    session_id: SessionFilter = None,
    limit: CallLimit = DEFAULT_CALL_LIMIT,
) -> list[LlmCall]:
    return llm_logging.recent_calls(
        user.id, s, purpose=purpose, errors_only=errors_only, session_id=session_id, limit=limit
    )


@router.get("/tool-calls")
def tool_calls(
    user: AdminUser, s: SessionDep, session_id: SessionFilter = None, limit: CallLimit = DEFAULT_CALL_LIMIT
) -> list[ToolCall]:
    return store.recent_tool_calls(user.id, s, session_id=session_id, limit=limit)
