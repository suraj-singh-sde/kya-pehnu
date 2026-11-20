"""Message history of a chat: rebuilt from the stored rows and cut to a window (LLD 3.3, 4.2)."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import Final

from sqlmodel import Session, col, select

from kyapehnu.agent.chat.limits import MAX_HISTORY_CHARS, MAX_HISTORY_MESSAGES
from kyapehnu.agent.chat.tools import known_name
from kyapehnu.agent.llm.base import (
    AssistantMsg,
    Msg,
    ToolCallRequest,
    ToolResultMsg,
    UserMsg,
    distinct_id,
    message_chars,
)
from kyapehnu.agent.models import ChatMessage, ChatRole, ToolCall

RESULT_OMITTED: Final = "[earlier result omitted]"


def load_turns(s: Session, session_id: int, before_turn: int) -> list[list[Msg]]:
    """The turns before `before_turn`, oldest first. A stored tool-call id that is empty, repeated or not valid for
    Anthropic is replayed as `call_<turn>_<step>_<n>`, the same for the call and its result (gov low 7).
    Every turn keeps its user message, so at most `MAX_HISTORY_MESSAGES - 1` earlier turns can fit the window."""
    first_turn = before_turn - MAX_HISTORY_MESSAGES
    messages = s.exec(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id, ChatMessage.turn < before_turn, ChatMessage.turn >= first_turn)
        .order_by(col(ChatMessage.turn), col(ChatMessage.step), col(ChatMessage.id))
    ).all()
    calls = s.exec(
        select(ToolCall)
        .where(ToolCall.session_id == session_id, ToolCall.turn < before_turn, ToolCall.turn >= first_turn)
        .order_by(col(ToolCall.turn), col(ToolCall.step), col(ToolCall.id))
    ).all()
    texts: dict[tuple[int, int], list[str]] = defaultdict(list)
    for m in messages:
        if m.role in (ChatRole.user, ChatRole.assistant):
            texts[m.turn, m.step].append(m.content)
    stored: dict[tuple[int, int], list[ToolCall]] = defaultdict(list)
    for c in calls:
        stored[c.turn, c.step].append(c)

    taken: set[str] = set()
    turns: dict[int, list[Msg]] = defaultdict(list)
    for turn, step in sorted(texts.keys() | stored.keys()):
        text = "\n".join(texts[turn, step])
        if step == 0:
            turns[turn].append(UserMsg(text))
            continue
        ids = [distinct_id(c.call_id, taken, f"call_{turn}_{step}", strict=True) for c in stored[turn, step]]
        requests = tuple(
            ToolCallRequest(i, known_name(c.name), c.args) for i, c in zip(ids, stored[turn, step], strict=True)
        )
        if text or requests:
            turns[turn].append(AssistantMsg(text, requests))
        turns[turn].extend(
            ToolResultMsg(i, known_name(c.name), c.result, c.error is not None)
            for i, c in zip(ids, stored[turn, step], strict=True)
        )
    return [turns[t] for t in sorted(turns)]


def _answered_ids(turn: Sequence[Msg], i: int) -> set[str]:
    """The call ids answered by the results that directly follow the message at `i`."""
    ids: set[str] = set()
    for m in turn[i + 1 :]:
        if not isinstance(m, ToolResultMsg):
            break
        ids.add(m.call_id)
    return ids


def _answered(turn: Sequence[Msg]) -> list[Msg]:
    """The turn without calls that have no result after them, and without an assistant message left empty."""
    out: list[Msg] = []
    for i, m in enumerate(turn):
        if isinstance(m, AssistantMsg) and m.tool_calls:
            followers = _answered_ids(turn, i)
            m = replace(m, tool_calls=tuple(c for c in m.tool_calls if c.id in followers))
        if not (isinstance(m, AssistantMsg) and not m.text and not m.tool_calls):
            out.append(m)
    return out


def _flatten(turn: Sequence[Msg]) -> list[Msg]:
    """For a model without tools: what was used is told as text, and no call or result is replayed (AC-51)."""
    out: list[Msg] = []
    for m in turn:
        if isinstance(m, AssistantMsg) and m.tool_calls:
            used = f"(used {', '.join(c.name for c in m.tool_calls)})"
            out.append(AssistantMsg(f"{m.text}\n{used}" if m.text else used))
        elif not isinstance(m, ToolResultMsg):
            out.append(m)
    return out


def _size(turn: Sequence[Msg]) -> int:
    return sum(map(message_chars, turn))


def _shrink(current: list[Msg]) -> list[Msg]:
    """Replace the oldest tool results by a marker until the turn fits, and no further."""
    out, over = list(current), _size(current) - MAX_HISTORY_CHARS
    for i, m in enumerate(out):
        if over <= 0:
            break
        if isinstance(m, ToolResultMsg) and len(m.content) > len(RESULT_OMITTED):
            over -= len(m.content) - len(RESULT_OMITTED)
            out[i] = replace(m, content=RESULT_OMITTED)
    return out


def window(earlier: Sequence[Sequence[Msg]], current: Sequence[Msg], tools_on: bool) -> list[Msg]:
    """`current` always, then whole earlier turns, newest first, while they fit in 40 messages and 60,000 characters.
    The walk stops at the first turn that does not fit."""
    prepare = _answered if tools_on else lambda t: _flatten(_answered(t))
    kept = _shrink(prepare(current))
    count, size = len(kept), _size(kept)
    older: list[list[Msg]] = []
    for turn in map(prepare, reversed(earlier)):
        count, size = count + len(turn), size + _size(turn)
        if count > MAX_HISTORY_MESSAGES or size > MAX_HISTORY_CHARS:
            break
        older.append(turn)
    return [m for turn in reversed(older) for m in turn] + kept
