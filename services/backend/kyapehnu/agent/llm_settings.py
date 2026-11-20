"""Choosing the LLM provider, key and model from the UI. Routes stay thin; the rules live here."""

import logging
import time
from collections.abc import Iterable
from typing import Final
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from kyapehnu.agent.llm import LlmConfig, known_secrets, presets, resolve, store, transport_for
from kyapehnu.agent.llm.base import LLMOutputError, LLMUnavailableError, Transport, redact
from kyapehnu.agent.llm.vision_check import VisionCheckError, check_vision
from kyapehnu.core.config import Settings
from kyapehnu.core.context import Ctx
from kyapehnu.core.errors import ConflictError, InvalidInputError

log = logging.getLogger(__name__)

UNUSABLE_REPLY: Final = "the model returned a reply that could not be used"  # never the reply itself: it may echo input
CUSTOM: Final = "custom"
KEY_HINT_CHARS: Final = 4
KEY_HINT_MIN_KEY_CHARS: Final = 12  # a hint from a shorter key would give too much of it away
MAX_FIELD_CHARS: Final = 500
MAX_MODEL_CHARS: Final = 200
ENV_MANAGED: Final = "LLM settings are managed by .env or environment variables"
UNKNOWN_PROVIDER: Final = "Unknown provider"
KEY_NEEDED: Final = "Enter an API key"
ADDRESS_NEEDED: Final = "Enter a server address starting with http:// or https://"
MODEL_NEEDED: Final = "Enter a model name"


class PresetView(BaseModel):
    id: str
    label: str
    kind: str
    needs_key: bool
    key_url: str | None
    default_model: str | None
    has_saved_key: bool
    saved_model: str | None


class LlmView(BaseModel):
    configured: bool
    managed_by_env: bool
    can_edit: bool
    active_provider: str | None
    model: str
    base_url: str | None
    key_last4: str | None
    providers: list[PresetView]


class LlmDraft(BaseModel):
    provider: str = Field(max_length=MAX_FIELD_CHARS)
    api_key: str | None = Field(default=None, max_length=MAX_FIELD_CHARS)
    base_url: str | None = Field(default=None, max_length=MAX_FIELD_CHARS)


class LlmChoice(LlmDraft):
    model: str = Field(min_length=1, max_length=MAX_MODEL_CHARS)


class ModelsOut(BaseModel):
    ok: bool
    models: list[str] = []
    error: str | None = None


class CheckOut(BaseModel):
    ok: bool
    error: str | None = None
    latency_ms: int


def failure_text(e: Exception, secrets: Iterable[str | None]) -> str:
    """What the UI may show. A model reply and exception texts of unknown origin can echo anything, so only a
    provider's own "unavailable" message is passed on, with the known keys blanked out."""
    if isinstance(e, VisionCheckError):
        return str(e)
    if isinstance(e, LLMOutputError):
        return UNUSABLE_REPLY
    if isinstance(e, LLMUnavailableError):
        return redact(str(e), secrets)
    return type(e).__name__


def current(settings: Settings, ctx: Ctx) -> tuple[LlmConfig, store.LlmStore]:
    saved = store.load(ctx.data_dir)
    return resolve(settings, saved), saved


def _key_hint(key: str | None) -> str | None:
    return key[-KEY_HINT_CHARS:] if key and len(key) >= KEY_HINT_MIN_KEY_CHARS else None


def view(cfg: LlmConfig, saved: store.LlmStore, ctx: Ctx, is_admin: bool) -> LlmView:
    """Only the admin may change the provider, and only the admin sees the key hint and the address."""
    return LlmView(
        configured=cfg.configured,
        managed_by_env=cfg.managed_by_env,
        can_edit=is_admin and not cfg.managed_by_env,
        active_provider=cfg.provider_id,
        model=ctx.provider.model,
        base_url=cfg.base_url if is_admin else None,
        key_last4=_key_hint(cfg.api_key) if is_admin else None,
        providers=[
            PresetView(
                id=p.id,
                label=p.label,
                kind=p.kind,
                needs_key=p.needs_key,
                key_url=p.key_url,
                default_model=p.default_model,
                has_saved_key=bool(p.id in saved.providers and saved.providers[p.id].api_key),
                saved_model=saved.providers[p.id].model if p.id in saved.providers else None,
            )
            for p in presets.PRESETS.values()
        ],
    )


def _candidate(
    settings: Settings, ctx: Ctx, draft: LlmDraft, model: str
) -> tuple[Transport, str, store.ProviderSecret]:
    """Validate a draft and build the transport it describes. Nothing is saved and no call is made."""
    cfg, saved = current(settings, ctx)
    if cfg.managed_by_env:
        raise ConflictError(ENV_MANAGED)
    model = model.strip()
    preset = presets.PRESETS.get(draft.provider)
    if preset is None:
        raise InvalidInputError(UNKNOWN_PROVIDER)
    previous = saved.providers.get(preset.id)
    api_key = (draft.api_key or "").strip() or (previous.api_key if previous else None)
    if preset.needs_key and not api_key:
        raise InvalidInputError(KEY_NEEDED)
    base_url = preset.base_url
    if preset.id == CUSTOM:  # the only preset whose address the user chooses
        base_url = (draft.base_url or "").strip() or (previous.base_url if previous else None)
        parts = urlsplit(base_url or "")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise InvalidInputError(ADDRESS_NEEDED)
    entry = store.ProviderSecret(api_key=api_key, model=model, base_url=base_url if preset.id == CUSTOM else None)
    return transport_for(preset.kind, api_key, base_url, model), preset.id, entry


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def list_models(settings: Settings, ctx: Ctx, draft: LlmDraft) -> ModelsOut:
    transport, provider_id, entry = _candidate(settings, ctx, draft, model="")
    try:
        return ModelsOut(ok=True, models=transport.list_models())
    except Exception as e:  # "this provider has no model list" is an answer for the UI, never a 500
        log.warning("event=llm_models_failed provider=%s error=%s", provider_id, type(e).__name__)
        return ModelsOut(ok=False, error=failure_text(e, (*ctx.secrets, entry.api_key)))


def _chosen(settings: Settings, ctx: Ctx, choice: LlmChoice) -> tuple[Transport, str, store.ProviderSecret]:
    if not choice.model.strip():
        raise InvalidInputError(MODEL_NEEDED)
    return _candidate(settings, ctx, choice, choice.model)


def test(user_id: int, settings: Settings, ctx: Ctx, choice: LlmChoice) -> CheckOut:
    transport, provider_id, entry = _chosen(settings, ctx, choice)
    start = time.perf_counter()
    try:
        check_vision(ctx.provider.with_transport(transport), user_id)
    except Exception as e:  # any failure is the answer to "does it work?"
        log.warning("event=llm_test_failed provider=%s error=%s", provider_id, type(e).__name__)
        return CheckOut(ok=False, error=failure_text(e, (*ctx.secrets, entry.api_key)), latency_ms=_elapsed_ms(start))
    return CheckOut(ok=True, latency_ms=_elapsed_ms(start))


def save(user_id: int, settings: Settings, ctx: Ctx, choice: LlmChoice) -> None:
    """Save only a model proven to read a photo, then switch the running app to it."""
    transport, provider_id, entry = _chosen(settings, ctx, choice)
    try:
        check_vision(ctx.provider.with_transport(transport), user_id)
    except Exception as e:
        log.warning("event=llm_settings_rejected provider=%s error=%s", provider_id, type(e).__name__)
        raise InvalidInputError(failure_text(e, (*ctx.secrets, entry.api_key))) from e
    saved = store.load(ctx.data_dir)
    saved.providers[provider_id] = entry
    saved.active = provider_id
    store.save(ctx.data_dir, saved)
    # Keep replaced keys too: a call still in flight can fail with the old key in its error text.
    ctx.secrets = tuple(dict.fromkeys((*ctx.secrets, *known_secrets(settings, saved))))
    ctx.provider.set_transport(transport)
    log.info("event=llm_settings_saved provider=%s model=%r", provider_id, entry.model)
