"""Which provider, key and model the app uses: .env / environment first, then the UI's choice, else none."""

from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from kyapehnu.agent.llm import presets, store
from kyapehnu.agent.llm.base import LLMProvider, Transport
from kyapehnu.core.config import Settings
from kyapehnu.core.context import Ctx

# Any of these set in .env or the environment means a developer manages the LLM settings by hand.
ENV_FIELDS: Final = frozenset({"llm_provider", "anthropic_api_key", "openai_api_key"})
KEYLESS: Final = "none"  # the OpenAI SDK refuses an empty key; servers without keys ignore it


@dataclass(frozen=True)
class LlmConfig:
    kind: str
    model: str
    api_key: str | None
    base_url: str | None
    provider_id: str | None  # preset id when chosen in the UI
    managed_by_env: bool
    configured: bool


def resolve(settings: Settings, saved: store.LlmStore) -> LlmConfig:
    if settings.model_fields_set & ENV_FIELDS:
        if settings.llm_provider == "anthropic":
            key = settings.anthropic_api_key
            return LlmConfig("anthropic", settings.anthropic_model, key, None, None, True, bool(key))
        return LlmConfig(
            "openai_compatible",
            settings.openai_model,
            settings.openai_api_key,
            settings.openai_base_url,
            None,
            True,
            True,
        )
    preset = presets.PRESETS.get(saved.active or "")
    entry = saved.providers.get(saved.active or "")
    if preset is None or entry is None:
        return LlmConfig("anthropic", settings.anthropic_model, None, None, None, False, False)
    return LlmConfig(preset.kind, entry.model, entry.api_key, entry.base_url or preset.base_url, preset.id, False, True)


def image_model(cfg: LlmConfig) -> str | None:
    """The model that generates clean pictures, when the configured provider is OpenAI itself."""
    openai_preset = presets.PRESETS["openai"]
    if cfg.provider_id == openai_preset.id:
        return openai_preset.image_model
    openai_host = urlsplit(openai_preset.base_url or "").hostname
    if cfg.managed_by_env and cfg.kind == "openai_compatible" and urlsplit(cfg.base_url or "").hostname == openai_host:
        return openai_preset.image_model
    return None


def transport_for(kind: str, api_key: str | None, base_url: str | None, model: str) -> Transport:
    if kind == "anthropic":
        from kyapehnu.agent.llm.anthropic import AnthropicTransport

        return AnthropicTransport(api_key, model)
    from kyapehnu.agent.llm.openai_compat import OpenAICompatTransport

    return OpenAICompatTransport(base_url or "", api_key or KEYLESS, model)


def build_transport(settings: Settings, saved: store.LlmStore | None = None) -> Transport:
    cfg = resolve(settings, saved if saved is not None else store.LlmStore())
    return transport_for(cfg.kind, cfg.api_key, cfg.base_url, cfg.model)


def known_secrets(settings: Settings, saved: store.LlmStore) -> tuple[str | None, ...]:
    """Every key that provider error text could echo back."""
    return (settings.anthropic_api_key, settings.openai_api_key, *(p.api_key for p in saved.providers.values()))


def active_provider(ctx: Ctx) -> LLMProvider | None:
    """The provider, or None when none is configured (then no call is attempted, AC-34)."""
    if ctx.settings is None or not resolve(ctx.settings, store.load(ctx.data_dir)).configured:
        return None
    return ctx.provider
