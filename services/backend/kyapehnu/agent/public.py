"""What the other domains may use from the AI agent (LLD 3.4).

Imports only `agent.llm*`, `agent.llm_settings` and `agent.turns`, never `agent.chat`: the chat tools import
`wardrobe.public`, and the wardrobe imports this module, so a link back into the chat would be an import cycle.
"""

from kyapehnu.agent.llm import active_provider, build_transport, image_model, known_secrets, resolve
from kyapehnu.agent.llm.base import LLMError, LLMOutputError, LLMProvider, LLMUnavailableError, Transport, redact
from kyapehnu.agent.llm.logging import db_logger, purge_llm_calls
from kyapehnu.agent.llm.store import load as load_llm_store
from kyapehnu.agent.llm_settings import UNUSABLE_REPLY, failure_text
from kyapehnu.agent.turns import make_turns

__all__ = [
    "UNUSABLE_REPLY",
    "LLMError",
    "LLMOutputError",
    "LLMProvider",
    "LLMUnavailableError",
    "Transport",
    "active_provider",
    "build_transport",
    "db_logger",
    "failure_text",
    "image_model",
    "known_secrets",
    "load_llm_store",
    "make_turns",
    "purge_llm_calls",
    "redact",
    "resolve",
]
