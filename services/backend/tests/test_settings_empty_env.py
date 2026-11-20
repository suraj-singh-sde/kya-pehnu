"""Compose passes `${VAR:-}` for every optional variable: an empty value must mean "not set", or the AI form in the app
would be locked by a provider variable nobody filled in."""

import pytest

from kyapehnu.core.config import Settings


@pytest.mark.parametrize("name", ["KYAPEHNU_LLM_PROVIDER", "ANTHROPIC_API_KEY", "KYAPEHNU_OPENAI_API_KEY"])
def test_an_empty_variable_is_not_a_setting(monkeypatch, name):
    monkeypatch.setenv(name, "")

    settings = Settings()

    assert settings.model_fields_set == set()


def test_a_filled_variable_is_a_setting(monkeypatch):
    monkeypatch.setenv("KYAPEHNU_LLM_PROVIDER", "openai_compatible")

    assert Settings().model_fields_set == {"llm_provider"}
