"""The turn loop: ask the model, run its tools, repeat until it answers or a limit is hit."""

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Final

from sqlmodel import Session

from kyapehnu.agent.chat import history, prompt, store, tools
from kyapehnu.agent.chat.limits import (
    EMPTY_REPLY_NOTICE,
    LIMIT_NOTICE,
    MAX_TOOL_CALLS,
    NO_RETRIES,
    NO_TOOLS_NOTICE,
    TURN_BUDGET_S,
)
from kyapehnu.agent.chat.schemas import TurnOut
from kyapehnu.agent.llm.base import (
    CHAT_ERROR_CHARS,
    AssistantMsg,
    LLMError,
    LLMProvider,
    Msg,
    ToolCallRequest,
    ToolResultMsg,
    ToolsUnsupportedError,
    UserMsg,
)
from kyapehnu.agent.llm_settings import failure_text
from kyapehnu.agent.models import ChatRole
from kyapehnu.core.context import Ctx
from kyapehnu.user.public import get_profile
from kyapehnu.wardrobe.public import ChatView

log = logging.getLogger(__name__)

PROVIDER_FAILED: Final = "The AI provider failed: {reason}. Your message is saved; send it again to retry."
PROVIDER_UNUSABLE: Final = "The AI provider's reply could not be used."


def provider_notice(e: Exception, secrets: tuple[str | None, ...]) -> str:
    """What the page shows for a failed model call: the provider's own sentence with the keys blanked out, never an
    unknown exception's text, which can echo anything."""
    if not isinstance(e, LLMError):
        return PROVIDER_UNUSABLE
    return PROVIDER_FAILED.format(reason=failure_text(e, secrets)[:CHAT_ERROR_CHARS].rstrip("."))


def run_turn(
    user_id: int,
    ctx: Ctx,
    provider: LLMProvider,
    session_id: int,
    text: str,
    now: datetime,
    clock: Callable[[], float] = time.monotonic,
) -> TurnOut:
    """Answer one message: ask the model, run the tools it calls, repeat until it answers or a limit is hit.

    The user's message is stored first. No database session is open while the model or a tool's slow work runs. A
    failure of the model becomes a notice in the chat. Any other failure (a full disk) stops the turn and is raised,
    with what was stored so far kept.
    """
    budget = tools.TurnBudget(deadline=clock() + TURN_BUDGET_S, clock=clock)
    started = time.perf_counter()
    turn = store.start_turn(user_id, ctx.engine, session_id, text)
    with Session(ctx.engine) as s:
        profile = get_profile(user_id, s)
        view = ChatView(session_id, store.session_event_ids(user_id, s, session_id), profile.home_city)
        earlier = history.load_turns(s, session_id, before_turn=turn)
    # SDK retries are off for every model call a tool makes too (the outfit pick), so a turn stays within TURN_MAX_S
    env = tools.ToolEnv(user_id, replace(ctx, provider=provider.with_max_retries(NO_RETRIES)), view, now, budget)
    tools_on = not provider.tools_disabled()
    notices = [] if tools_on else [NO_TOOLS_NOTICE]
    current: list[Msg] = [UserMsg(text)]
    outcome, step = "", 0
    try:
        while not outcome:
            if not budget.can_call_model():
                outcome = "limit_reached"
                break
            step += 1
            system, messages = prompt.system(profile, now, tools_on), history.window(earlier, current, tools_on)
            try:
                result = provider.chat(
                    system=system,
                    messages=messages,
                    tools=tools.SPECS if tools_on else (),
                    label=f"session={session_id} turn={turn}",
                    first_call=budget.model_calls == 0,
                    user_id=user_id,
                )
            except Exception as e:  # LLMError or unexpected: the provider has logged it
                budget.model_calls += 1
                if isinstance(e, ToolsUnsupportedError) and budget.model_calls == 1 and tools_on:
                    tools_on, step = False, step - 1  # the first request only: ask again without tools (FR-3)
                    notices.append(NO_TOOLS_NOTICE)
                    continue
                notices.append(provider_notice(e, (*ctx.secrets, getattr(provider.transport, "api_key", None))))
                outcome = "provider_error"
                break
            budget.model_calls += 1
            calls = result.tool_calls if tools_on else ()
            if result.text:
                store.add_message(user_id, ctx.engine, session_id, turn, step, ChatRole.assistant, result.text)
            if not calls:
                if not result.text:
                    notices.append(EMPTY_REPLY_NOTICE)
                outcome = "ok" if tools_on else "no_tools"
                break
            ran: list[tuple[ToolCallRequest, ToolResultMsg]] = []
            for call in calls:
                if budget.tool_calls >= MAX_TOOL_CALLS or not budget.time_left():
                    outcome = "limit_reached"  # the rest are neither run nor stored
                    break
                budget.tool_calls += 1
                ran.append((call, tools.run_call(env, call, session_id, turn, step)))
            answered = tuple(
                replace(c, name=tools.known_name(c.name)) for c, _ in ran
            )  # only the calls that were answered
            current.append(AssistantMsg(result.text, answered))
            current.extend(r for _, r in ran)
            if env.looks_shown and not outcome:
                outcome = "ok"  # the looks say it all: the page hides the prose of a turn that showed them (no call)
        if outcome == "limit_reached":
            notices.append(LIMIT_NOTICE)
        for notice in notices:
            store.add_message(user_id, ctx.engine, session_id, turn, step + 1, ChatRole.notice, notice)
    finally:
        store.finish_turn(ctx.engine, session_id)
        log.info(
            "event=chat_turn session_id=%s turn=%s model_calls=%d tool_calls=%d outcome=%s latency_ms=%d",
            session_id,
            turn,
            budget.model_calls,
            budget.tool_calls,
            outcome or "failed",
            (time.perf_counter() - started) * 1000,
        )
    with Session(ctx.engine) as s:
        return TurnOut(outcome=outcome, turn=turn, session=store.session_view(user_id, s, session_id, now.date()))
