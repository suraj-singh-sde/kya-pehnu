"""Fixed limits of the chat (LLD 6) and the two texts the turn loop adds to a chat."""

from typing import Final

from kyapehnu.agent.llm import anthropic, openai_compat

MAX_MODEL_CALLS: Final = 8  # per turn: chat calls, the no-tools retry and PICK_CALLS per generate_outfits
PICK_CALLS: Final = 2  # charged before generate_outfits runs
MAX_GENERATE_RUNS: Final = 2  # per turn
MAX_TOOL_CALLS: Final = 12  # per turn; calls beyond it are neither run nor stored
NO_RETRIES: Final = 0  # SDK retries of every model call inside a turn (R10)
TURN_BUDGET_S: Final = 600  # checked before every model call and tool call
# The longest model call of a turn: SDK retries are 0 inside turns, so a call is one request.
CALL_TIMEOUT_S: Final = max(openai_compat.REQUEST_TIMEOUT_SECONDS, anthropic.REQUEST_TIMEOUT_SECONDS)
# The budget is checked before each model and tool call; the longest work started after the last check is one
# generate_outfits: the judge's parallel decision calls (one timeout, together) and at most PICK_CALLS pick calls.
TURN_MAX_S: Final = TURN_BUDGET_S + (PICK_CALLS + 1) * CALL_TIMEOUT_S
TURN_LOCK_TTL_S: Final = TURN_MAX_S + 60  # nginx /api/chat/ read and send timeouts equal it
MAX_HISTORY_MESSAGES: Final = 40
MAX_HISTORY_CHARS: Final = 60_000
MAX_RESULT_CHARS: Final = 12_000  # per tool result
MAX_SEARCH_RESULTS: Final = 50
MAX_MESSAGE_CHARS: Final = 4000
TITLE_CHARS: Final = 60
SESSION_LIST_LIMIT: Final = 500  # ponytail: add paging when someone keeps more than 500 chats

NO_TOOLS_NOTICE: Final = (
    "This model cannot use tools, so it cannot see your wardrobe. Pick a tool-capable model in Settings."
)
LIMIT_NOTICE: Final = "I stopped here: this reply reached its limit of steps. Send a message to continue."
EMPTY_REPLY_NOTICE: Final = "The model sent an empty reply. Send your message again."
