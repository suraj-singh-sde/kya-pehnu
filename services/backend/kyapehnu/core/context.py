"""The per-app bundle of engine, LLM provider, preferences and clients that services receive."""

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Engine

from kyapehnu.core.config import Prefs, Settings
from kyapehnu.files.public import FileStore, LocalStore

if TYPE_CHECKING:
    from kyapehnu.agent.llm.base import LLMProvider
    from kyapehnu.weather.openmeteo import WeatherClient


@dataclass
class Ctx:
    engine: Engine
    provider: "LLMProvider"
    prefs: Prefs
    data_dir: Path
    secrets: tuple[str | None, ...] = ()  # API keys to blank out of provider error text shown to clients
    settings: Settings | None = None  # which provider is configured decides whether clean pictures are made
    weather: "WeatherClient | None" = None  # None: no weather (tests, or a build without one)
    file_store: FileStore | None = None  # None: the disk under data_dir (tests that build a Ctx by hand)

    @cached_property
    def files(self) -> FileStore:
        """The picture store every caller goes through."""
        return LocalStore(self.data_dir) if self.file_store is None else self.file_store
