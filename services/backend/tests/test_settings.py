"""Settings (LLD 6): paths, public URL, Google, Valkey, session, rate limit and docs, all from KYAPEHNU_* variables."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from kyapehnu.core.config import Settings


def make(**fields) -> Settings:
    return Settings(_env_file=None, **fields)


def test_the_defaults_suit_a_container():
    s = make()

    assert (s.data_dir, s.config_file) == (Path("/data"), Path("/data/config.yaml"))
    assert (s.public_origin, s.public_host, s.public_https) == ("http://localhost:8080", "localhost", False)
    assert (s.valkey_url, s.valkey_password, s.session_idle_days) == ("redis://localhost:6379/0", None, 30)
    assert (s.auth_rate_limit, s.docs_enabled, s.google_configured) == ("20/minute", False, False)


def test_the_config_file_follows_the_data_dir_unless_given(tmp_path):
    assert make(data_dir=tmp_path).config_file == tmp_path / "config.yaml"
    assert make(data_dir=tmp_path, config_file=tmp_path / "x.yaml").config_file == tmp_path / "x.yaml"


def test_settings_come_from_kyapehnu_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KYAPEHNU_PUBLIC_URL", "https://kyapehnu.example")
    monkeypatch.setenv("KYAPEHNU_DOCS_ENABLED", "true")
    monkeypatch.setenv("KYAPEHNU_SESSION_IDLE_DAYS", "7")

    s = make()

    assert (s.data_dir, s.public_https, s.docs_enabled, s.session_idle_days) == (tmp_path, True, True, 7)


@pytest.mark.parametrize(
    ("raw", "origin"),
    [
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://localhost:8080/", "http://localhost:8080"),
        ("https://Kyapehnu.Example", "https://kyapehnu.example"),
    ],
)
def test_the_public_url_is_kept_as_an_origin(raw, origin):
    assert make(public_url=raw).public_origin == origin


@pytest.mark.parametrize(
    "raw",
    [
        "localhost:8080",
        "ftp://localhost",
        "http://",
        "http://localhost/app",
        "http://localhost?x=1",
        "http://localhost#x",
        "http://user:pw@localhost",
    ],
)
def test_a_public_url_that_is_not_a_plain_origin_is_refused(raw):
    with pytest.raises(ValidationError):
        make(public_url=raw)


def test_the_public_host_is_lowercased_and_joins_the_allowlist():
    s = make(public_url="https://Kyapehnu.Example:8443")

    assert s.public_host == "kyapehnu.example"
    assert s.allowed_hosts == {"kyapehnu.example", "localhost", "127.0.0.1"}


def test_google_is_configured_only_with_both_values():
    assert not make(google_client_id="id").google_configured
    assert not make(google_client_secret="secret").google_configured
    assert not make(google_client_id="id", google_client_secret="").google_configured
    assert make(google_client_id="id", google_client_secret="secret").google_configured


def test_the_secrets_are_hidden_in_a_repr():
    s = make(google_client_secret="the-secret", valkey_password="the-password")

    assert isinstance(s.google_client_secret, SecretStr)
    assert "the-secret" not in repr(s) and "the-password" not in repr(s)


def test_a_valkey_url_with_a_credential_is_refused():
    with pytest.raises(ValidationError):
        make(valkey_url="redis://:pw@valkey:6379/0")


@pytest.mark.parametrize("days", [0, 366])
def test_session_idle_days_stay_between_1_and_365(days):
    with pytest.raises(ValidationError):
        make(session_idle_days=days)


@pytest.mark.parametrize("rate", ["20/minute", "5 per second", "100/hour"])
def test_a_valid_rate_is_accepted(rate):
    assert make(auth_rate_limit=rate).auth_rate_limit == rate


@pytest.mark.parametrize("rate", ["", "fast", "20", "x/minute"])
def test_an_invalid_rate_is_refused(rate):
    with pytest.raises(ValidationError):
        make(auth_rate_limit=rate)


def test_a_refused_valkey_url_does_not_print_its_credential():
    with pytest.raises(ValidationError) as caught:
        make(valkey_url="redis://:hunter2-credential@valkey:6379/0")

    assert "hunter2-credential" not in str(caught.value)
