"""The CLI's picture-store plumbing (work plan, rules for every package): `_store()` builds the store from the
settings, and a store outage in any command ends as one fixed line on stderr and exit 1, never a traceback and never
a key, address or bucket name (NFR-6)."""

import pytest

from kyapehnu.core import cli
from kyapehnu.core.config import get_settings
from kyapehnu.files.errors import StoreUnavailableError
from kyapehnu.files.local import LocalStore
from kyapehnu.files.s3 import S3Store
from tests.conftest import TEST_BUCKET

MESSAGE = "files_unavailable: Pictures are unavailable right now. Retry shortly.\n"


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYAPEHNU_CONFIG_FILE", str(tmp_path / "config.yaml"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_store_is_local_when_no_bucket_is_set(settings_env, tmp_path):
    store = cli._store()

    assert isinstance(store, LocalStore)
    assert (tmp_path / "data" / "images").is_dir()


def test_store_is_s3_when_a_bucket_is_set(settings_env, monkeypatch, s3):
    monkeypatch.setenv("KYAPEHNU_FILES_BUCKET", TEST_BUCKET)
    get_settings.cache_clear()

    assert isinstance(cli._store(), S3Store)


def _fail_with(error):
    def command(_args):
        raise error

    return command


def test_a_store_outage_in_a_command_prints_one_line_and_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_migrate", _fail_with(StoreUnavailableError()))

    with pytest.raises(SystemExit) as stop:
        cli.main(["migrate"])

    out, err = capsys.readouterr()
    assert stop.value.code == 1
    assert err == MESSAGE
    assert out == ""


def test_the_line_never_carries_the_text_of_the_error(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "cmd_migrate", _fail_with(StoreUnavailableError("s3://secret-bucket/images/7/thumb/a.webp 10.0.0.5"))
    )

    with pytest.raises(SystemExit):
        cli.main(["migrate"])

    assert capsys.readouterr().err == MESSAGE


def test_other_errors_are_not_swallowed_by_the_handler(monkeypatch):
    monkeypatch.setattr(cli, "cmd_migrate", _fail_with(RuntimeError("a bug")))

    with pytest.raises(RuntimeError, match="a bug"):
        cli.main(["migrate"])


def test_a_refusal_still_exits_2(monkeypatch, capsys):
    from kyapehnu.core.errors import StartupRefusedError

    monkeypatch.setattr(cli, "cmd_migrate", _fail_with(StartupRefusedError("not a Kya Pehnu database")))

    with pytest.raises(SystemExit) as stop:
        cli.main(["migrate"])

    assert stop.value.code == cli.REFUSED_EXIT_CODE
    assert "not a Kya Pehnu database" in capsys.readouterr().err
