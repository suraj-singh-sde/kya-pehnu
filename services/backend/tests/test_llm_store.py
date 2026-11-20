import logging
import os
import stat

import pytest

from kyapehnu.agent.llm import store


def _saved():
    return store.LlmStore(
        active="openai",
        providers={
            "openai": store.ProviderSecret(api_key="sk-oai-1234567890", model="m-1"),
            "custom": store.ProviderSecret(api_key=None, model="m-2", base_url="http://box:1234/v1"),
        },
    )


def test_round_trip(tmp_path):
    store.save(tmp_path, _saved())

    assert store.load(tmp_path) == _saved()


def test_missing_file_loads_empty(tmp_path):
    assert store.load(tmp_path) == store.LlmStore()


@pytest.mark.parametrize("content", ["{not json", '{"providers": {"x": {}}}', "[]"])
def test_corrupt_store_loads_empty(tmp_path, content, caplog):
    (tmp_path / store.FILE_NAME).write_text(content)

    with caplog.at_level(logging.WARNING):
        assert store.load(tmp_path) == store.LlmStore()

    assert any("event=llm_store_unreadable" in r.getMessage() for r in caplog.records)
    assert content not in caplog.text  # never echo the file: it may hold a key


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_file_is_private_even_when_replacing_a_wider_one(tmp_path):
    path = tmp_path / store.FILE_NAME
    path.write_text("{}")
    path.chmod(0o644)

    store.save(tmp_path, _saved())

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_leaves_no_temp_file(tmp_path):
    store.save(tmp_path, _saved())

    assert [p.name for p in tmp_path.iterdir()] == [store.FILE_NAME]


def test_failed_save_keeps_the_previous_file(tmp_path, monkeypatch):
    store.save(tmp_path, _saved())
    monkeypatch.setattr(store.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError):
        store.save(tmp_path, store.LlmStore())

    monkeypatch.undo()
    assert store.load(tmp_path) == _saved()
    assert [p.name for p in tmp_path.iterdir()] == [store.FILE_NAME]
