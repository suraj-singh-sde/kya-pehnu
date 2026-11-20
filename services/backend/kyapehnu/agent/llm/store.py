"""The LLM provider, key and model chosen in the UI: <data_dir>/secrets.json.

Kept out of the database on purpose, so a backup never carries a key.
ponytail: the key is plain text in a private file; move to the OS keychain with the native installers.
"""

import logging
import os
from pathlib import Path
from typing import Final

from pydantic import BaseModel

log = logging.getLogger(__name__)

FILE_NAME: Final = "secrets.json"
PRIVATE_MODE: Final = 0o600


class ProviderSecret(BaseModel):
    api_key: str | None = None
    model: str
    base_url: str | None = None


class LlmStore(BaseModel):
    active: str | None = None
    providers: dict[str, ProviderSecret] = {}


def load(data_dir: Path) -> LlmStore:
    """An unreadable or hand-broken file means "not set up yet", never a crash at startup."""
    try:
        return LlmStore.model_validate_json((data_dir / FILE_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LlmStore()
    except (OSError, ValueError) as e:  # pydantic's ValidationError is a ValueError
        log.warning("event=llm_store_unreadable error=%s", type(e).__name__)
        return LlmStore()


def save(data_dir: Path, saved: LlmStore) -> None:
    path = data_dir / FILE_NAME
    tmp = path.with_suffix(".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(saved.model_dump_json(indent=2))
        os.chmod(tmp, PRIVATE_MODE)  # O_CREAT keeps the mode of a temp file left by an earlier crash
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
