import logging
import time

from fastapi import APIRouter
from pydantic import BaseModel

from kyapehnu.agent import llm_settings
from kyapehnu.agent.llm import image_model
from kyapehnu.agent.llm_settings import CheckOut, LlmChoice, LlmDraft, LlmView, ModelsOut
from kyapehnu.auth.public import AdminUser, CurrentUser
from kyapehnu.core.config import OutfitPrefs, RepetitionPrefs, WeatherPrefs
from kyapehnu.core.deps import CtxDep, SettingsDep

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsOut(BaseModel):
    provider: str
    model: str
    llm_configured: bool
    is_admin: bool  # the page shows the AI call log link and the provider form to the admin only
    region: str
    home_city: str
    currency: str
    clean_pictures: bool  # the provider can generate clean garment pictures
    auto_clean_pictures: bool  # and does so for every upload, so a new piece is not ready until it has one
    llm: LlmView
    outfits: OutfitPrefs  # read-only: edited in config.yaml
    repetition: RepetitionPrefs
    weather: WeatherPrefs


class TestLlmOut(BaseModel):
    ok: bool
    error: str | None = None
    latency_ms: int


class Ping(BaseModel):
    ok: bool


def _view(user: CurrentUser, settings: SettingsDep, ctx: CtxDep) -> LlmView:
    cfg, saved = llm_settings.current(settings, ctx)
    return llm_settings.view(cfg, saved, ctx, user.is_admin)


@router.get("")
def get_settings_info(user: CurrentUser, settings: SettingsDep, ctx: CtxDep) -> SettingsOut:
    cfg, saved = llm_settings.current(settings, ctx)
    return SettingsOut(
        provider=cfg.kind,
        model=ctx.provider.model,
        llm_configured=cfg.configured,
        is_admin=user.is_admin,
        region=ctx.prefs.region,
        home_city=ctx.prefs.home_city,
        currency=ctx.prefs.currency,
        clean_pictures=image_model(cfg) is not None,
        auto_clean_pictures=ctx.prefs.ingest.auto_clean_pictures,
        llm=llm_settings.view(cfg, saved, ctx, user.is_admin),
        outfits=ctx.prefs.outfits,
        repetition=ctx.prefs.repetition,
        weather=ctx.prefs.weather,
    )


@router.post("/llm/models")
def llm_models(body: LlmDraft, user: AdminUser, settings: SettingsDep, ctx: CtxDep) -> ModelsOut:
    return llm_settings.list_models(settings, ctx, body)


@router.post("/llm/test")
def llm_test(body: LlmChoice, user: AdminUser, settings: SettingsDep, ctx: CtxDep) -> CheckOut:
    return llm_settings.test(user.id, settings, ctx, body)


@router.put("/llm")
def llm_save(body: LlmChoice, user: AdminUser, settings: SettingsDep, ctx: CtxDep) -> LlmView:
    llm_settings.save(user.id, settings, ctx, body)
    return _view(user, settings, ctx)


@router.post("/test-llm")
def test_llm(user: AdminUser, ctx: CtxDep) -> TestLlmOut:
    start = time.perf_counter()
    try:
        out = ctx.provider.complete_json(
            system="You are a connectivity check.",
            prompt='Reply with {"ok": true}.',
            images=[],
            schema=Ping,
            purpose="test",
            user_id=user.id,
        )
    except Exception as e:  # any provider failure is the answer to "does it work?", never a 500
        log.warning("event=test_llm_failed error=%s", type(e).__name__)
        error = llm_settings.failure_text(e, ctx.secrets)
        return TestLlmOut(ok=False, error=error, latency_ms=int((time.perf_counter() - start) * 1000))
    return TestLlmOut(ok=out.ok, latency_ms=int((time.perf_counter() - start) * 1000))
