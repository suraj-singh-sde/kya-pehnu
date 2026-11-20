"""The providers offered in Setup. Model names are never listed here: the UI asks the provider."""

from dataclasses import dataclass
from typing import Final, Literal

from kyapehnu.core.config import Settings

Kind = Literal["anthropic", "openai_compatible"]


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    kind: Kind
    base_url: str | None  # None: the SDK default (Claude) or entered by the user (custom)
    needs_key: bool
    key_url: str | None  # where a person gets a key
    default_model: str | None = None
    image_model: str | None = None  # generates clean garment pictures (kyapehnu.wardrobe.pictures)


_PRESETS: Final = (
    Preset(
        "claude",
        "Claude (Anthropic)",
        "anthropic",
        None,
        True,
        "https://console.anthropic.com/settings/keys",
        Settings.model_fields["anthropic_model"].default,
    ),
    Preset(
        "openai",
        "OpenAI",
        "openai_compatible",
        "https://api.openai.com/v1",
        True,
        "https://platform.openai.com/api-keys",
        "gpt-4o-2024-08-06",  # chosen by the project owner for photo tagging; check it at each release
        "gpt-image-2-2026-04-21",  # chosen by the project owner for clean pictures; check it at each release
    ),
    Preset(
        "gemini",
        "Gemini (Google)",
        "openai_compatible",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        True,
        "https://aistudio.google.com/apikey",
    ),
    Preset(
        "openrouter",
        "OpenRouter",
        "openai_compatible",
        "https://openrouter.ai/api/v1",
        True,
        "https://openrouter.ai/keys",
    ),
    Preset(
        "ollama",
        "Ollama (on this computer)",
        "openai_compatible",
        Settings.model_fields["openai_base_url"].default,
        False,
        "https://ollama.com/download",
        Settings.model_fields["openai_model"].default,
    ),
    Preset("custom", "Other (OpenAI-compatible server)", "openai_compatible", None, False, None),
)
PRESETS: Final[dict[str, Preset]] = {p.id: p for p in _PRESETS}
