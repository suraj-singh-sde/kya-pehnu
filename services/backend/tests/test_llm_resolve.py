import pytest

from kyapehnu.agent.llm import build_transport, known_secrets, presets, resolve, store
from kyapehnu.agent.llm.anthropic import AnthropicTransport
from kyapehnu.agent.llm.openai_compat import OpenAICompatTransport
from kyapehnu.core.config import Settings

ENV_NAMES = ("KYAPEHNU_LLM_PROVIDER", "ANTHROPIC_API_KEY", "KYAPEHNU_ANTHROPIC_API_KEY", "KYAPEHNU_OPENAI_API_KEY")


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path, config_file=tmp_path / "missing.yaml")


def _saved(active="openai"):
    return store.LlmStore(
        active=active,
        providers={
            "openai": store.ProviderSecret(api_key="sk-oai-1234567890", model="m-1"),
            "ollama": store.ProviderSecret(api_key=None, model="local-1"),
            "custom": store.ProviderSecret(api_key="k", model="m-2", base_url="http://box:1234/v1"),
        },
    )


def test_presets_cover_the_approved_providers():
    assert list(presets.PRESETS) == ["claude", "openai", "gemini", "openrouter", "ollama", "custom"]
    assert presets.PRESETS["claude"].kind == "anthropic"
    assert presets.PRESETS["claude"].default_model == Settings.model_fields["anthropic_model"].default
    assert presets.PRESETS["ollama"].needs_key is False
    assert presets.PRESETS["custom"].base_url is None
    assert all(
        p.base_url.startswith("https://")
        for p in presets.PRESETS.values()
        if p.id in {"openai", "gemini", "openrouter"}
    )


def test_nothing_configured(tmp_path, clean_env):
    cfg = resolve(_settings(tmp_path), store.LlmStore())

    assert (cfg.configured, cfg.managed_by_env, cfg.provider_id, cfg.api_key) == (False, False, None, None)
    assert cfg.kind == "anthropic"


def test_store_choice_is_used_with_the_preset_address(tmp_path, clean_env):
    cfg = resolve(_settings(tmp_path), _saved())

    assert (cfg.configured, cfg.managed_by_env, cfg.provider_id) == (True, False, "openai")
    assert (cfg.kind, cfg.model, cfg.api_key, cfg.base_url) == (
        "openai_compatible",
        "m-1",
        "sk-oai-1234567890",
        presets.PRESETS["openai"].base_url,
    )


def test_custom_uses_its_saved_address(tmp_path, clean_env):
    assert resolve(_settings(tmp_path), _saved("custom")).base_url == "http://box:1234/v1"


@pytest.mark.parametrize("active", ["gone-provider", "gemini", None])
def test_unknown_active_provider_is_unconfigured(tmp_path, clean_env, active):
    assert resolve(_settings(tmp_path), _saved(active)).configured is False


def test_env_key_makes_config_env_managed(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")

    cfg = resolve(_settings(tmp_path), _saved())

    assert (cfg.managed_by_env, cfg.configured, cfg.kind, cfg.api_key) == (True, True, "anthropic", "sk-ant-from-env")
    assert cfg.provider_id is None


def test_env_provider_openai_compatible_keeps_phase_1_behaviour(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("KYAPEHNU_LLM_PROVIDER", "openai_compatible")

    cfg = resolve(_settings(tmp_path), store.LlmStore())

    assert (cfg.managed_by_env, cfg.configured, cfg.kind) == (True, True, "openai_compatible")
    assert cfg.base_url == Settings.model_fields["openai_base_url"].default


def test_build_transport_from_store(tmp_path, clean_env):
    t = build_transport(_settings(tmp_path), _saved())

    assert isinstance(t, OpenAICompatTransport)
    assert (t.model, t.base_url, t.api_key) == ("m-1", presets.PRESETS["openai"].base_url, "sk-oai-1234567890")


def test_keyless_server_gets_a_placeholder_key(tmp_path, clean_env):
    t = build_transport(_settings(tmp_path), _saved("ollama"))

    assert isinstance(t, OpenAICompatTransport) and t.api_key  # the OpenAI SDK refuses an empty key


def test_build_transport_unconfigured_is_keyless_anthropic(tmp_path, clean_env):
    t = build_transport(_settings(tmp_path))

    assert isinstance(t, AnthropicTransport) and t.api_key is None


def test_known_secrets_cover_env_and_every_saved_key(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")

    secrets = known_secrets(_settings(tmp_path), _saved())

    assert {"sk-ant-from-env", "sk-oai-1234567890", "k"} <= set(secrets)


def test_openai_starts_on_the_owner_chosen_vision_model():
    assert presets.PRESETS["openai"].default_model == "gpt-4o-2024-08-06"
