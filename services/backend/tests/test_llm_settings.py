import logging
import os
import stat

import pytest
from fastapi.testclient import TestClient

from kyapehnu.agent import llm_settings
from kyapehnu.agent.llm import presets, store, vision_check
from kyapehnu.agent.llm.base import LLMUnavailableError
from kyapehnu.agent.llm.openai_compat import OpenAICompatTransport
from kyapehnu.core.config import Settings
from tests.conftest import LOOPBACK_PEER
from tests.fakes import FakeTransport

KEY = "sk-oai-ABCDEFGH-1234CDEF"
SEES = {"left": "red", "right": "blue"}
BLIND = {"left": "green", "right": "green"}
CHOICE = {"provider": "openai", "api_key": KEY, "model": "vision-1"}
ENV_NAMES = ("KYAPEHNU_LLM_PROVIDER", "ANTHROPIC_API_KEY", "KYAPEHNU_ANTHROPIC_API_KEY", "KYAPEHNU_OPENAI_API_KEY")


@pytest.fixture
def ui_settings(tmp_path, monkeypatch):
    """Nothing in the environment: the UI manages the LLM settings."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, data_dir=tmp_path / "data", config_file=tmp_path / "missing.yaml")


@pytest.fixture
def ui_app(ui_settings, fake):
    from kyapehnu.core.app import create_app

    return create_app(settings=ui_settings, transport=fake)


@pytest.fixture
def ui_client(ui_app):
    with TestClient(ui_app, base_url="http://localhost", client=LOOPBACK_PEER) as c:
        yield c


@pytest.fixture
def candidates(monkeypatch):
    """Capture what the service would connect to, and hand back scripted transports instead."""
    monkeypatch.setattr(vision_check, "make_probe", lambda rng=None: (b"\x89PNG-probe", "red", "blue"))
    made = []

    def install(*transports):
        queue = list(transports)

        def fake_transport_for(kind, api_key, base_url, model):
            t = queue.pop(0)
            t.model, t.api_key = model, api_key
            made.append({"kind": kind, "api_key": api_key, "base_url": base_url, "model": model, "transport": t})
            return t

        monkeypatch.setattr(llm_settings, "transport_for", fake_transport_for)
        return made

    return install


def _llm(client):
    return client.get("/api/settings").json()["llm"]


def test_fresh_app_is_unconfigured_and_editable(ui_client):
    body = ui_client.get("/api/settings").json()

    assert body["llm_configured"] is False
    llm = body["llm"]
    assert (llm["configured"], llm["managed_by_env"], llm["can_edit"]) == (False, False, True)
    assert (llm["active_provider"], llm["key_last4"]) == (None, None)
    assert [p["id"] for p in llm["providers"]] == list(presets.PRESETS)
    assert all(p["has_saved_key"] is False for p in llm["providers"])


def test_save_stores_the_choice_and_switches_the_live_provider(ui_client, ui_settings, candidates):
    made = candidates(FakeTransport([SEES]))

    r = ui_client.put("/api/settings/llm", json=CHOICE)

    assert r.status_code == 200, r.text
    assert KEY not in r.text
    llm = r.json()
    assert (llm["configured"], llm["active_provider"], llm["model"], llm["key_last4"]) == (
        True,
        "openai",
        "vision-1",
        "CDEF",
    )
    assert made[0]["kind"] == "openai_compatible" and made[0]["base_url"] == presets.PRESETS["openai"].base_url
    saved = store.load(ui_settings.data_dir)
    assert saved.active == "openai" and saved.providers["openai"].api_key == KEY
    assert ui_client.app.state.ctx.provider.transport is made[0]["transport"]
    assert KEY in ui_client.app.state.ctx.secrets
    assert ui_client.get("/api/settings").json()["llm_configured"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_saved_file_is_private(ui_client, ui_settings, candidates):
    candidates(FakeTransport([SEES]))

    ui_client.put("/api/settings/llm", json=CHOICE)

    assert stat.S_IMODE((ui_settings.data_dir / store.FILE_NAME).stat().st_mode) == 0o600


def test_a_model_that_cannot_see_is_not_saved(ui_client, ui_settings, candidates):
    candidates(FakeTransport([BLIND]))
    before = ui_client.app.state.ctx.provider.transport

    r = ui_client.put("/api/settings/llm", json=CHOICE)

    assert r.status_code == 400 and r.json()["code"] == "invalid_input"
    assert "could not read the test photo" in r.json()["detail"]
    assert not (ui_settings.data_dir / store.FILE_NAME).exists()
    assert ui_client.app.state.ctx.provider.transport is before
    assert _llm(ui_client)["configured"] is False


def test_provider_error_on_save_never_echoes_the_submitted_key(ui_client, ui_settings, candidates):
    candidates(FakeTransport([LLMUnavailableError(f"401 invalid key {KEY}")]))

    r = ui_client.put("/api/settings/llm", json=CHOICE)

    assert r.status_code == 400 and KEY not in r.text and "401 invalid key ***" in r.json()["detail"]
    assert not (ui_settings.data_dir / store.FILE_NAME).exists()


def test_test_endpoint_reports_without_saving(ui_client, ui_settings, candidates):
    candidates(FakeTransport([SEES]), FakeTransport([BLIND]), FakeTransport([RuntimeError(f"boom {KEY}")]))

    ok = ui_client.post("/api/settings/llm/test", json=CHOICE).json()
    blind = ui_client.post("/api/settings/llm/test", json=CHOICE).json()
    broken = ui_client.post("/api/settings/llm/test", json=CHOICE)

    assert ok["ok"] is True and ok["error"] is None
    assert blind["ok"] is False and "could not read the test photo" in blind["error"]
    assert broken.status_code == 200 and broken.json() == {
        "ok": False,
        "error": "RuntimeError",
        "latency_ms": broken.json()["latency_ms"],
    }
    assert KEY not in broken.text
    assert not (ui_settings.data_dir / store.FILE_NAME).exists()


def test_models_are_listed_with_the_submitted_key(ui_client, candidates):
    made = candidates(FakeTransport(models=["vision-1", "text-1"]))

    r = ui_client.post("/api/settings/llm/models", json={"provider": "openai", "api_key": KEY})

    assert r.json() == {"ok": True, "models": ["vision-1", "text-1"], "error": None}
    assert made[0]["api_key"] == KEY


def test_models_failure_is_reported_not_a_500(ui_client, candidates):
    candidates(FakeTransport(models=LLMUnavailableError(f"404 no model list for {KEY}")))

    r = ui_client.post("/api/settings/llm/models", json={"provider": "openai", "api_key": KEY})

    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["models"] == [] and KEY not in r.text


def test_saved_key_is_reused_when_none_is_sent(ui_client, candidates):
    made = candidates(FakeTransport([SEES]), FakeTransport(models=["a"]), FakeTransport([SEES]))
    ui_client.put("/api/settings/llm", json=CHOICE)

    ui_client.post("/api/settings/llm/models", json={"provider": "openai"})
    r = ui_client.put("/api/settings/llm", json={"provider": "openai", "model": "vision-2"})

    assert r.status_code == 200 and r.json()["model"] == "vision-2"
    assert [m["api_key"] for m in made] == [KEY, KEY, KEY]


def test_each_provider_keeps_its_own_key(ui_client, ui_settings, candidates):
    candidates(FakeTransport([SEES]), FakeTransport([SEES]))
    ui_client.put("/api/settings/llm", json=CHOICE)

    ui_client.put(
        "/api/settings/llm", json={"provider": "claude", "api_key": "sk-ant-ZZZZZZZZZZZZ9999", "model": "c-1"}
    )

    saved = store.load(ui_settings.data_dir)
    assert saved.active == "claude" and saved.providers["openai"].api_key == KEY
    providers = {p["id"]: p for p in _llm(ui_client)["providers"]}
    assert providers["openai"]["has_saved_key"] and providers["openai"]["saved_model"] == "vision-1"


def test_pasted_key_is_trimmed(ui_client, ui_settings, candidates):
    made = candidates(FakeTransport([SEES]))

    ui_client.put("/api/settings/llm", json={**CHOICE, "api_key": f"  {KEY}\n"})

    assert made[0]["api_key"] == KEY and store.load(ui_settings.data_dir).providers["openai"].api_key == KEY


def test_short_key_has_no_hint(ui_client, candidates):
    candidates(FakeTransport([SEES]))

    r = ui_client.put("/api/settings/llm", json={**CHOICE, "api_key": "short-key"})

    assert r.status_code == 200 and r.json()["key_last4"] is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"provider": "nope", "api_key": KEY, "model": "m"}, "Unknown provider"),
        ({"provider": "openai", "model": "m"}, "Enter an API key"),
        ({"provider": "custom", "model": "m"}, "http://"),
        ({"provider": "custom", "base_url": "ftp://box/v1", "model": "m"}, "http://"),
        ({"provider": "custom", "base_url": "file:///etc/passwd", "model": "m"}, "http://"),
    ],
)
def test_invalid_choices_are_rejected_before_any_call(ui_client, candidates, body, message):
    made = candidates()

    r = ui_client.put("/api/settings/llm", json=body)

    assert r.status_code == 400 and message in r.json()["detail"] and made == []


def test_empty_or_oversized_model_is_a_422(ui_client):
    assert ui_client.put("/api/settings/llm", json={**CHOICE, "model": ""}).status_code == 422
    assert ui_client.put("/api/settings/llm", json={**CHOICE, "model": "m" * 201}).status_code == 422


def test_custom_server_address_is_used_and_shown_to_the_admin(ui_client, candidates):
    made = candidates(FakeTransport([SEES]))

    r = ui_client.put(
        "/api/settings/llm", json={"provider": "custom", "base_url": " http://box:1234/v1 ", "model": "m"}
    )

    assert r.status_code == 200 and made[0]["base_url"] == "http://box:1234/v1"
    assert _llm(ui_client)["base_url"] == "http://box:1234/v1"


def test_a_non_admin_view_hides_the_key_hint_and_the_address(ui_client, ui_settings, candidates):
    candidates(FakeTransport([SEES]))
    ui_client.put("/api/settings/llm", json=CHOICE)
    ctx = ui_client.app.state.ctx
    cfg, saved = llm_settings.current(ui_settings, ctx)

    seen = llm_settings.view(cfg, saved, ctx, False)

    assert seen.configured is True
    assert (seen.can_edit, seen.key_last4, seen.base_url) == (False, None, None)
    assert KEY not in seen.model_dump_json() and "CDEF" not in seen.model_dump_json()


def test_env_managed_settings_are_read_only(client, settings, candidates):
    made = candidates(FakeTransport([SEES]))

    llm = _llm(client)
    responses = [
        client.put("/api/settings/llm", json=CHOICE),
        client.post("/api/settings/llm/test", json=CHOICE),
        client.post("/api/settings/llm/models", json={"provider": "openai", "api_key": KEY}),
    ]

    assert (llm["managed_by_env"], llm["can_edit"], llm["configured"]) == (True, False, True)
    assert [r.status_code for r in responses] == [409, 409, 409]
    assert made == [] and not (settings.data_dir / store.FILE_NAME).exists()


def test_the_choice_survives_a_restart(ui_settings, fake, candidates):
    from kyapehnu.core.app import create_app

    candidates(FakeTransport([SEES]))
    with TestClient(
        create_app(settings=ui_settings, transport=fake), base_url="http://localhost", client=LOOPBACK_PEER
    ) as c:
        c.put("/api/settings/llm", json=CHOICE)

    app = create_app(settings=ui_settings, start_worker=False)  # no injected transport: built from the store

    t = app.state.ctx.provider.transport
    assert isinstance(t, OpenAICompatTransport)
    assert (t.model, t.api_key, t.base_url) == ("vision-1", KEY, presets.PRESETS["openai"].base_url)
    assert KEY in app.state.ctx.secrets


def test_next_tagging_call_uses_the_new_provider(ui_client, candidates):
    from tests.conftest import noise_image_bytes
    from tests.fakes import analysis, garment

    made = candidates(FakeTransport([SEES, analysis(garment())]))
    ui_client.put("/api/settings/llm", json=CHOICE)

    r = ui_client.post("/api/ingest", files=[("files", ("a.jpg", noise_image_bytes(5), "image/jpeg"))])
    ui_client.app.state.worker.drain()

    assert r.status_code == 201
    assert [c["name"] for c in made[0]["transport"].calls] == ["ColorHalves", "PhotoAnalysis"]


def test_the_key_never_reaches_the_logs(ui_client, candidates, caplog):
    candidates(FakeTransport([SEES]), FakeTransport([LLMUnavailableError(f"bad {KEY}")]))

    with caplog.at_level(logging.DEBUG):
        ui_client.put("/api/settings/llm", json=CHOICE)
        ui_client.put("/api/settings/llm", json=CHOICE)

    assert KEY not in caplog.text
    assert KEY not in ui_client.get("/api/debug/llm-calls").text  # nor the call log, which is in backups
    assert "event=llm_settings_saved provider=openai" in caplog.text
    assert "event=llm_settings_rejected provider=openai" in caplog.text


def test_existing_test_llm_redacts_ui_saved_keys(ui_client, candidates):
    t = FakeTransport([SEES, LLMUnavailableError(f"401 {KEY}")])
    candidates(t)
    ui_client.put("/api/settings/llm", json=CHOICE)

    r = ui_client.post("/api/settings/test-llm")

    assert r.json()["ok"] is False and KEY not in r.text


def test_replaced_key_stays_on_the_redaction_list(ui_client, candidates):
    candidates(FakeTransport([SEES]), FakeTransport([SEES]))
    ui_client.put("/api/settings/llm", json=CHOICE)

    ui_client.put("/api/settings/llm", json={**CHOICE, "api_key": "sk-oai-SECOND-KEY-9999"})

    secrets = ui_client.app.state.ctx.secrets  # a call still in flight may fail with the old key in its text
    assert KEY in secrets and "sk-oai-SECOND-KEY-9999" in secrets


def test_model_name_is_trimmed(ui_client, ui_settings, candidates):
    made = candidates(FakeTransport([SEES]))

    r = ui_client.put("/api/settings/llm", json={**CHOICE, "model": "  vision-1 \n"})

    assert r.status_code == 200 and made[0]["model"] == "vision-1"
    assert store.load(ui_settings.data_dir).providers["openai"].model == "vision-1"


def test_blank_model_name_is_rejected_before_any_call(ui_client, candidates):
    made = candidates()

    r = ui_client.put("/api/settings/llm", json={**CHOICE, "model": "   "})

    assert r.status_code == 400 and "Enter a model name" in r.json()["detail"] and made == []
