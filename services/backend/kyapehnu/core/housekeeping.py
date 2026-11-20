"""Startup housekeeping: purge old LLM call logs and sweep image files no row refers to. Never blocks startup."""

import logging
from collections.abc import Callable

from sqlalchemy import Engine

from kyapehnu.agent.public import purge_llm_calls
from kyapehnu.files.public import FileStore
from kyapehnu.wardrobe.public import sweep_orphans

log = logging.getLogger(__name__)


def _guarded(step: str, action: Callable[[], object]) -> None:
    try:
        action()
    except Exception as e:  # housekeeping is best effort and must never block startup
        log.error("event=housekeeping_failed step=%s error=%s", step, type(e).__name__)
        log.debug("event=housekeeping_failed_trace step=%s", step, exc_info=True)


def run(engine: Engine, store: FileStore, max_deletes: int) -> None:
    _guarded("purge_llm_calls", lambda: purge_llm_calls(engine))
    _guarded("sweep_orphans", lambda: sweep_orphans(engine, store, max_deletes))
