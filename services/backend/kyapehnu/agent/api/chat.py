from fastapi import APIRouter, Response

from kyapehnu.agent.chat import orchestrator, proposals, store
from kyapehnu.agent.chat.schemas import MessageIn, ProposalView, SessionOut, SessionSummary, TurnOut
from kyapehnu.agent.public import active_provider
from kyapehnu.auth.public import CurrentUser
from kyapehnu.core import deps
from kyapehnu.core.deps import CtxDep, PathId, SessionDep, TurnsDep
from kyapehnu.core.errors import NoProviderError

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.get("/sessions")
def list_sessions(user: CurrentUser, s: SessionDep) -> list[SessionSummary]:
    return store.list_sessions(user.id, s)


@router.post("/sessions", status_code=201)
def create_session(user: CurrentUser, ctx: CtxDep) -> SessionSummary:
    return store.create_session(user.id, ctx.engine)


@router.get("/sessions/{session_id}")
def get_session(session_id: PathId, user: CurrentUser, s: SessionDep) -> SessionOut:
    return store.session_view(user.id, s, session_id, deps.local_now().date())


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: PathId, user: CurrentUser, s: SessionDep, ctx: CtxDep, turns: TurnsDep) -> Response:
    store.require_session(user.id, s, session_id)  # before the lock: another account learns nothing about it
    with turns.hold(session_id):
        store.delete_session(user.id, ctx.engine, session_id)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/messages")
def send_message(
    session_id: PathId, body: MessageIn, user: CurrentUser, s: SessionDep, ctx: CtxDep, turns: TurnsDep
) -> TurnOut:
    provider = active_provider(ctx)
    if provider is None:  # before the lock and before anything is stored
        raise NoProviderError()
    store.require_session(user.id, s, session_id)
    s.close()  # no database session stays open while the model works
    with turns.hold(session_id):
        return orchestrator.run_turn(user.id, ctx, provider, session_id, body.text, deps.local_now())


@router.post("/proposals/{tool_call_id}/confirm")
def confirm_proposal(tool_call_id: PathId, user: CurrentUser, ctx: CtxDep) -> ProposalView:
    return proposals.confirm(user.id, ctx, tool_call_id, deps.local_now().date())


@router.post("/proposals/{tool_call_id}/cancel")
def cancel_proposal(tool_call_id: PathId, user: CurrentUser, ctx: CtxDep) -> ProposalView:
    return proposals.cancel(user.id, ctx.engine, tool_call_id, deps.local_now().date())
