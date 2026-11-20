"""The nine tools of the chat and the budget of a turn (LLD 3.5).

The model only names a tool and sends arguments. Every argument goes through the Pydantic model of the REST route, a
tool never raises into the turn (a failure becomes an error result the model can read), and what the model is told is
built by the allow-list views of `chat/privacy.py`.
"""

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final, Literal, Self, cast

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from kyapehnu.agent.chat import store
from kyapehnu.agent.chat.limits import (
    MAX_GENERATE_RUNS,
    MAX_MODEL_CALLS,
    MAX_RESULT_CHARS,
    MAX_SEARCH_RESULTS,
    PICK_CALLS,
)
from kyapehnu.agent.chat.privacy import event_view, item_view, outfit_view, wear_view, weather_view
from kyapehnu.agent.llm.base import TOOL_NAME_CHARS, ToolCallRequest, ToolResultMsg, ToolSpec
from kyapehnu.agent.models import ProposalState, ToolCall
from kyapehnu.core.context import Ctx
from kyapehnu.core.errors import (
    DB_BUSY_MARKERS,
    InvalidInputError,
    ItemNotInOutfitError,
    NoAlternativeError,
    NotFoundError,
    PlaceNotFoundError,
    UnavailableError,
)
from kyapehnu.core.limits import MAX_EXCLUDE, MAX_INCLUDE, MAX_WEARS
from kyapehnu.core.schemas import DistinctIds, ItemIds, RowId
from kyapehnu.wardrobe.public import (
    ChatView,
    Event,
    EventDraft,
    FeedbackIn,
    ItemStatus,
    SuggestIn,
    SuggestionOut,
    WardrobeQuery,
    WearLogOut,
    WearsQuery,
    WeatherQuery,
    create_event,
    event_from_draft,
    find_events,
    get_event,
    get_item,
    give_feedback,
    item_history,
    list_items,
    list_wears,
    require_owned_items,
    suggest,
    weather_report,
)

log = logging.getLogger(__name__)

PROPOSAL_TEXT: Final = "Shown to the user as a card with Confirm and Cancel. Nothing changes until they press Confirm."
WAITING: Final = "Waiting for your confirmation"
INTERNAL_MESSAGE: Final = "The tool failed."
LIMIT_MESSAGE: Final = "No more outfit generations in this turn."
UNKNOWN_TOOL: Final = "unknown_tool"
NOT_AN_OBJECT: Final = "Arguments must be a JSON object."
CUT_MARKER: Final = "[cut: {removed} characters]"
CUT_MARKER_FIXED_CHARS: Final = len(CUT_MARKER.format(removed=""))

# What a domain error is called when the model reads it. Any other exception is an internal error.
DOMAIN_CODES: Final[tuple[tuple[type[Exception], str], ...]] = (
    (InvalidInputError, "invalid_arguments"),
    (ItemNotInOutfitError, "invalid_arguments"),
    (PlaceNotFoundError, "place_not_found"),
    (NotFoundError, "not_found"),
    (NoAlternativeError, "no_alternative"),
)

ToolKind = Literal["read", "direct_write", "proposal"]


class ToolError(Exception):
    """A failure the model gets to read: `{"error": {"code", "message"}}`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class TurnBudget:
    """What one turn may still spend: model calls (a pick counts two), tool calls, generate runs, time."""

    deadline: float
    clock: Callable[[], float] = time.monotonic
    model_calls: int = 0
    tool_calls: int = 0
    generate_runs: int = 0

    def time_left(self) -> bool:
        return self.clock() < self.deadline

    def can_call_model(self) -> bool:
        return self.model_calls < MAX_MODEL_CALLS and self.time_left()

    def reserve_pick(self) -> bool:
        """Charge the two model calls of the pick inside generate_outfits before it runs. False changes nothing."""
        if self.generate_runs >= MAX_GENERATE_RUNS or MAX_MODEL_CALLS - self.model_calls < PICK_CALLS:
            return False
        self.model_calls += PICK_CALLS
        self.generate_runs += 1
        return True


@dataclass
class ToolEnv:
    user_id: int  # the caller: every wardrobe call and every stored row of a tool is theirs
    ctx: Ctx
    view: ChatView
    now: datetime  # the chat's clock: "today" is `now.date()`
    budget: TurnBudget
    looks_shown: bool = (
        False  # generate_outfits returned looks this turn: the turn ends on them (the page hides the prose)
    )


@dataclass(frozen=True)
class ToolOutput:
    model: dict[str, Any]  # what the model is told
    ui: dict[str, Any] | None = None  # what the page is built from (an error has none)
    event_id: int | None = None
    proposal: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    kind: ToolKind
    run: Callable[[ToolEnv, Session, Any], ToolOutput]


class GetItemArgs(BaseModel):
    item_id: RowId


class GenerateArgs(BaseModel):
    event: EventDraft | None = None  # the occasion, described here: saved with the looks, one round-trip
    event_id: RowId | None = None  # or an event made earlier in this chat
    exclude: list[ItemIds] = Field(default=[], max_length=MAX_EXCLUDE)
    include: Annotated[DistinctIds, Field(max_length=MAX_INCLUDE)] = []

    @model_validator(mode="after")
    def _one_event_source(self) -> Self:
        if (self.event is None) == (self.event_id is None):
            raise InvalidInputError("send exactly one of event and event_id")
        return self


class LogWearArgs(BaseModel):
    item_ids: ItemIds
    event_id: RowId | None = None


class StatusArgs(BaseModel):
    item_id: RowId
    status: ItemStatus


# --- the tools: each runs in a session of its own --------------------------------------------------------------------


def _summary(text: str) -> dict[str, Any]:
    return {"summary": text}


def _events_by_id(env: ToolEnv, s: Session, wears: Sequence[WearLogOut]) -> dict[int, Event]:
    return find_events(env.user_id, s, {w.event_id for w in wears if w.event_id is not None})


def _search_wardrobe(env: ToolEnv, s: Session, q: WardrobeQuery) -> ToolOutput:
    # the brand is not in any view, so the model must not be able to probe it with `q` (gov low 3)
    found = list_items(env.user_id, s, q, brand=False, today=env.now.date())
    shown = [item_view(i) for i in found[:MAX_SEARCH_RESULTS]]
    return ToolOutput({"total": len(found), "items": shown}, _summary(f"Found {len(found)} items"))


def _get_item(env: ToolEnv, s: Session, a: GetItemArgs) -> ToolOutput:
    item = get_item(env.user_id, s, a.item_id)
    wears, outfits = item_history(env.user_id, s, a.item_id)
    wears = wears[:MAX_WEARS]
    known = _events_by_id(env, s, wears)
    model = {
        "item": item_view(item),
        "wears": [wear_view(w, known, env.view) for w in wears],
        "outfits": [outfit_view(o) for o in outfits],
    }
    return ToolOutput(model, _summary(f"Looked at {item.name}"))


def _suggestion_for_model(number: int, x: SuggestionOut) -> dict[str, Any]:
    return {
        "number": number,
        "item_ids": x.item_ids,
        "items": [{"id": i.id, "name": i.name, "category": i.category} for i in x.items],
        "reason": x.reason,
        "notes": x.notes,
        "swap_item_id": x.swap.item_id if x.swap else None,
    }


def _suggestion_for_page(x: SuggestionOut) -> dict[str, Any]:
    return {
        "item_ids": x.item_ids,
        "reason": x.reason,
        "notes": x.notes,
        "swap_item_id": x.swap.item_id if x.swap else None,
    }


def _generate_outfits(env: ToolEnv, s: Session, a: GenerateArgs) -> ToolOutput:
    if not env.budget.reserve_pick():
        raise ToolError("limit_reached", LIMIT_MESSAGE)
    if a.event is not None:  # the weather is fetched first: an unknown place saves nothing
        body = SuggestIn(event=event_from_draft(a.event, env.view.home_city), exclude=a.exclude, include=a.include)
    else:
        body = SuggestIn(event_id=a.event_id, exclude=a.exclude, include=a.include)
    out = suggest(env.user_id, env.ctx, s, body, env.now.date(), view=env.view)
    event_id = cast(int, out.event.id)
    if a.event is not None:
        env.view.session_event_ids.add(event_id)  # this chat made the event: its title and place may be shown
    if out.suggestions:
        env.looks_shown = True
    problem = out.problem
    model = {
        "event_id": event_id,
        "event": event_view(out.event, env.view),
        "weather": weather_view(out.weather),
        "problem": {"cause": problem.cause, "message": problem.message} if problem else None,
        "suggestions": [_suggestion_for_model(n, x) for n, x in enumerate(out.suggestions, start=1)],
    }
    ui = {
        "kind": "outfits",
        "event_id": event_id,
        "weather": out.weather.model_dump(mode="json") if out.weather else None,
        "problem": problem.model_dump(mode="json") if problem else None,
        "suggestions": [_suggestion_for_page(x) for x in out.suggestions],
        "summary": f"{len(out.suggestions)} outfits",
    }
    return ToolOutput(model, ui, event_id if a.event is not None else None)


def _get_weather(env: ToolEnv, s: Session, q: WeatherQuery) -> ToolOutput:
    found = weather_report(env.user_id, env.ctx, s, q, env.now)
    return ToolOutput({"place": found.place, "weather": weather_view(found.weather)}, _summary("Weather looked up"))


def _get_wear_history(env: ToolEnv, s: Session, q: WearsQuery) -> ToolOutput:
    wears = list_wears(env.user_id, s, q, env.now.date())
    known = _events_by_id(env, s, wears)
    shown = [wear_view(w, known, env.view) for w in wears]
    return ToolOutput({"wears": shown}, _summary(f"{len(wears)} wears"))


def _log_wear(env: ToolEnv, s: Session, a: LogWearArgs) -> ToolOutput:
    found = require_owned_items(env.user_id, s, a.item_ids)
    for item_id in a.item_ids:
        away = found[item_id]
        if away.status != ItemStatus.available:  # a proposal that no Confirm could carry out is a dead card
            raise ToolError("not_available", f"{away.name} is {away.status}. Only available items can be worn.")
    if a.event_id is not None:
        get_event(env.user_id, s, a.event_id)
    ui = {"kind": "log_wear", "item_ids": a.item_ids, "event_id": a.event_id, "summary": WAITING}
    return ToolOutput({"status": "awaiting_confirmation", "message": PROPOSAL_TEXT}, ui, proposal=True)


def _record_feedback(env: ToolEnv, s: Session, body: FeedbackIn) -> ToolOutput:
    saved = give_feedback(env.user_id, env.ctx, s, body)
    thumb = "up" if saved.rating > 0 else "down"
    return ToolOutput({"outfit_id": saved.outfit_id, "rating": saved.rating}, _summary(f"Thumbs {thumb} saved"))


def _update_item_status(env: ToolEnv, s: Session, a: StatusArgs) -> ToolOutput:
    item = get_item(env.user_id, s, a.item_id)
    if item.status == a.status:
        raise ToolError("invalid_arguments", f"The item is already {a.status.value}.")
    ui = {
        "kind": "update_item_status",
        "item_id": a.item_id,
        "from": str(item.status),
        "to": a.status.value,
        "summary": WAITING,
    }
    return ToolOutput({"status": "awaiting_confirmation", "message": PROPOSAL_TEXT}, ui, proposal=True)


def _create_event(env: ToolEnv, s: Session, draft: EventDraft) -> ToolOutput:
    event = create_event(env.user_id, s, draft, env.view.home_city)
    env.view.session_event_ids.add(cast(int, event.id))  # before the view is built: this chat made the event (H1)
    return ToolOutput(event_view(event, env.view), {"kind": "event", **_summary("Event created")}, event.id)


REGISTRY: Final[dict[str, Tool]] = {
    t.name: t
    for t in (
        Tool(
            "search_wardrobe",
            "Search the user's wardrobe; filters combine. Returns up to 50 items and the total. "
            "Only status 'available' items can be worn.",
            WardrobeQuery,
            "read",
            _search_wardrobe,
        ),
        Tool(
            "get_item",
            "One item with its recent wears and the saved outfits that contain it.",
            GetItemArgs,
            "read",
            _get_item,
        ),
        Tool(
            "generate_outfits",
            "Up to three outfits from the wardrobe for an occasion, fewer when fewer are good. Send the event "
            "inline (times local, without offset: YYYY-MM-DDTHH:MM; omit place for the user's home city; "
            "type festival for Diwali, Holi, Eid, Navratri or Christmas, party for a birthday or house party), or the "
            "event_id of an event made earlier in this conversation. The weather is looked up for the event. "
            "exclude: item-id sets already shown. include: item ids every outfit must contain.",
            GenerateArgs,
            "read",
            _generate_outfits,
        ),
        Tool(
            "get_weather",
            "Weather summary and clothing notes for a place and local time. Omit place for the user's home city.",
            WeatherQuery,
            "read",
            _get_weather,
        ),
        Tool(
            "get_wear_history",
            "Wears in the last N days of one item (item_id) or one outfit (item_ids).",
            WearsQuery,
            "read",
            _get_wear_history,
        ),
        Tool(
            "log_wear",
            "Propose logging an outfit as worn today. The user confirms it on screen.",
            LogWearArgs,
            "proposal",
            _log_wear,
        ),
        Tool(
            "record_feedback",
            "Save the user's rating of an outfit: 1 thumbs up, -1 thumbs down, by item_ids or outfit_id, "
            "with an optional reason.",
            FeedbackIn,
            "direct_write",
            _record_feedback,
        ),
        Tool(
            "update_item_status",
            "Propose changing one item's status. The user confirms it on screen.",
            StatusArgs,
            "proposal",
            _update_item_status,
        ),
        Tool(
            "create_event",
            "Save an occasion the user describes. Times are local, without offset (YYYY-MM-DDTHH:MM). "
            "Omit place for the user's home city. Returns the event id for generate_outfits.",
            EventDraft,
            "direct_write",
            _create_event,
        ),
    )
}


# --- the schemas the model reads ------------------------------------------------------------------------------------


def _flat(node: Any, defs: dict[str, Any]) -> Any:
    """`node` with every `$ref` replaced by its definition and every schema `title` dropped."""
    if isinstance(node, list):
        return [_flat(n, defs) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = defs[node["$ref"].rsplit("/", 1)[-1]]
        return _flat({**target, **{k: v for k, v in node.items() if k != "$ref"}}, defs)
    return {
        k: {name: _flat(sub, defs) for name, sub in v.items()} if k == "properties" else _flat(v, defs)
        for k, v in node.items()
        if k != "title"  # the keyword, not a property that is called title
    }


def schema(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema of a tool's arguments with no `$ref`, `$defs` or titles: small models read this flat form."""
    raw = model.model_json_schema()
    flat = cast(dict[str, Any], _flat(raw, raw.get("$defs", {})))
    for key in ("$defs", "description"):  # the tool's own description is sent beside the schema
        flat.pop(key, None)
    return flat


SPECS: Final[tuple[ToolSpec, ...]] = tuple(ToolSpec(t.name, t.description, schema(t.args)) for t in REGISTRY.values())


# --- run_call -------------------------------------------------------------------------------------------------------


def _describe(e: ValidationError) -> str:
    """`field (error type)` for each failure, joined by `; `. Our own rules add their static text. Never a value."""
    parts: list[str] = []
    for err in e.errors(include_input=False, include_url=False):
        where = ".".join(str(p) for p in err["loc"])
        part = f"{where} ({err['type']})" if where else f"({err['type']})"
        cause = err.get("ctx", {}).get("error")
        if err["type"] == "value_error" and isinstance(cause, InvalidInputError):
            part = f"{part}: {cause}"
        parts.append(part)
    return "; ".join(parts)


def known_name(name: str) -> str:
    """The name a call is stored and replayed under: a name the model made up never leaves the result text."""
    return name if name in REGISTRY else UNKNOWN_TOOL


def _prepare(call: ToolCallRequest) -> tuple[Tool, BaseModel]:
    tool = REGISTRY.get(call.name)
    if tool is None:
        names = ", ".join(REGISTRY)
        raise ToolError(UNKNOWN_TOOL, f"No tool named {call.name[:TOOL_NAME_CHARS]}. Tools: {names}.")
    if call.arguments is None:
        raise ToolError("invalid_arguments", NOT_AN_OBJECT)
    try:
        return tool, tool.args.model_validate(call.arguments)
    except ValidationError as e:
        raise ToolError("invalid_arguments", _describe(e)) from e


def _is_busy(e: OperationalError) -> bool:
    return any(marker in str(e.orig).lower() for marker in DB_BUSY_MARKERS)


def _run(env: ToolEnv, tool: Tool, args: BaseModel) -> ToolOutput:
    """Run the tool in its own short session. A busy database and the failures of a domain rule go back to the model.

    A full disk or any other OS or database error is not the model's to retry: it is raised, and the turn stops.
    """
    try:
        with Session(env.ctx.engine, expire_on_commit=False) as s:  # a busy error after the commit must not read
            return tool.run(env, s, args)
    except (ToolError, OSError):
        raise
    except OperationalError as e:
        if not _is_busy(e):
            raise
        raise ToolError("db_busy", UnavailableError.message) from e
    except Exception as e:
        code = next((code for kind, code in DOMAIN_CODES if isinstance(e, kind)), None)
        if code is not None:
            raise ToolError(code, str(e)) from e
        log.error(
            "event=tool_failed name=%s error=%s", tool.name, type(e).__name__
        )  # the class only: text may echo input
        raise ToolError("internal_error", INTERNAL_MESSAGE) from e


def _removed_for(length: int) -> int:
    """How many characters to cut from a result of `length` so that the marker, which names that number, fits."""
    digits = 1
    while True:
        removed = length - MAX_RESULT_CHARS + CUT_MARKER_FIXED_CHARS + digits
        if len(str(removed)) == digits:
            return removed
        digits += 1


def _bounded(content: str) -> tuple[str, bool]:
    if len(content) <= MAX_RESULT_CHARS:
        return content, False
    marker = CUT_MARKER.format(removed=_removed_for(len(content)))
    return content[: MAX_RESULT_CHARS - len(marker)] + marker, True


def run_call(env: ToolEnv, call: ToolCallRequest, session_id: int, turn: int, step: int) -> ToolResultMsg:
    """Run one call of the model, store it and return the result the model is sent. A bad call is an error result."""
    start = time.perf_counter()
    stored_args = call.arguments
    error: ToolError | None = None
    try:
        tool, args = _prepare(call)
        stored_args = args.model_dump(mode="json")
        output = _run(env, tool, args)
    except ToolError as e:
        error = e
        output = ToolOutput({"error": {"code": e.code, "message": e.message}})
    content, truncated = _bounded(json.dumps(output.model, ensure_ascii=False, separators=(",", ":"), default=str))
    latency_ms = round((time.perf_counter() - start) * 1000)
    today = env.now.date()
    store.add_tool_call(
        env.ctx.engine,
        ToolCall(
            user_id=env.user_id,
            session_id=session_id,
            turn=turn,
            step=step,
            call_id=call.id,
            name=known_name(call.name),
            args=stored_args,
            result=content,
            truncated=truncated,
            error=error.code if error else None,
            latency_ms=latency_ms,
            event_id=output.event_id,
            ui=output.ui,
            proposal_state=ProposalState.pending.value if output.proposal else None,
            proposed_on=today if output.proposal else None,
        ),
    )
    outcome = error.code if error else "proposal" if output.proposal else "ok"
    known = call.name if call.name in REGISTRY else "unknown"
    log.info("event=tool_call session_id=%s name=%s outcome=%s latency_ms=%d", session_id, known, outcome, latency_ms)
    return ToolResultMsg(call.id, known_name(call.name), content, is_error=error is not None)
