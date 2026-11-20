import json
import logging
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest
from pydantic import BaseModel
from sqlmodel import Session, select

from kyapehnu.agent import llm
from kyapehnu.agent.llm.anthropic import AnthropicTransport
from kyapehnu.agent.llm.base import ChatResult, LLMOutputError, LLMProvider, LLMUnavailableError
from kyapehnu.agent.llm.logging import db_logger
from kyapehnu.agent.llm.openai_compat import OpenAICompatTransport
from kyapehnu.agent.models import LlmCall
from kyapehnu.core.config import Settings
from kyapehnu.core.db import make_engine, migrate
from tests.fakes import OWNER_ID, FakeTransport, ensure_owner


class Ping(BaseModel):
    ok: bool


def _provider(replies):
    logs = []
    return LLMProvider(FakeTransport(replies), log=logs.append), logs


def _ask(p, **kw):
    return p.complete_json(system="s", prompt="p", images=[], schema=Ping, purpose="t", **{"user_id": OWNER_ID, **kw})


def test_valid_reply_parsed_and_logged():
    p, logs = _provider([{"ok": True}])
    assert p.complete_json(system="s", prompt="p", images=[], schema=Ping, purpose="test", user_id=OWNER_ID) == Ping(
        ok=True
    )
    assert len(logs) == 1 and logs[0].error is None and logs[0].purpose == "test"
    assert logs[0].input_tokens == 10
    assert (p.provider_name, p.model) == ("fake", "fake-1")


def test_code_fenced_json_accepted():
    p, _ = _provider(['```json\n{"ok": false}\n```'])
    assert _ask(p).ok is False


def test_invalid_then_valid_retries_with_error_in_prompt():
    p, logs = _provider(['{"ok": "maybe"}', {"ok": True}])
    assert _ask(p).ok
    assert len(logs) == 2 and logs[0].error and logs[1].error is None
    assert "previous reply was invalid" in p.transport.calls[1]["prompt"]


def test_invalid_twice_raises_output_error():
    p, logs = _provider(["nope", "still nope"])
    with pytest.raises(LLMOutputError):
        _ask(p)
    assert len(logs) == 2


def test_unavailable_raises_without_retry():
    p, logs = _provider([LLMUnavailableError("down")])
    with pytest.raises(LLMUnavailableError):
        _ask(p)
    assert len(logs) == 1 and "down" in logs[0].error


def test_unexpected_transport_error_is_logged_and_propagates():
    p, logs = _provider([KeyError("boom")])
    with pytest.raises(KeyError):
        _ask(p)
    assert len(logs) == 1 and "KeyError" in logs[0].error


def test_log_sink_failure_does_not_break_call_or_leak_text(caplog):
    def bad_sink(_):
        raise RuntimeError("INSERT parameters: secret-response-text")

    p = LLMProvider(FakeTransport([{"ok": True}]), log=bad_sink)
    with caplog.at_level(logging.INFO):
        assert _ask(p).ok
    assert "secret-response-text" not in caplog.text
    assert "event=llm_log_failed" in caplog.text


def test_call_log_has_no_image_bytes():
    p, logs = _provider([{"ok": True}])
    p.complete_json(system="s", prompt="p", images=[b"RAWIMAGEBYTES"], schema=Ping, purpose="t", user_id=OWNER_ID)
    assert "RAWIMAGEBYTES" not in logs[0].request_summary
    assert "[13] bytes" in logs[0].request_summary


def test_failed_attempts_emit_llm_call_failed_event(caplog):
    p, _ = _provider(["profile-secret-one", "profile-secret-two"])
    with caplog.at_level(logging.INFO), pytest.raises(LLMOutputError):
        _ask(p)
    events = [r.getMessage() for r in caplog.records if "event=llm_call_failed" in r.getMessage()]
    assert events == ["event=llm_call_failed purpose=t provider=fake error=invalid_output"] * 2
    assert "profile-secret" not in caplog.text


def test_unavailable_emits_llm_call_failed_with_class_only(caplog):
    p, _ = _provider([LLMUnavailableError("sk-ant-leaky-key")])
    with caplog.at_level(logging.INFO), pytest.raises(LLMUnavailableError):
        _ask(p)
    assert "event=llm_call_failed purpose=t provider=fake error=LLMUnavailableError" in caplog.text
    assert "sk-ant-leaky-key" not in caplog.text


def test_success_emits_no_failure_event(caplog):
    p, _ = _provider([{"ok": True}])
    with caplog.at_level(logging.INFO):
        _ask(p)
    assert "llm_call_failed" not in caplog.text


def test_db_logger_persists_call(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)
    with Session(engine) as s:
        ensure_owner(s)
        s.commit()
    p = LLMProvider(FakeTransport([{"ok": True}]), log=db_logger(engine))
    _ask(p)
    with Session(engine) as s:
        (row,) = s.exec(select(LlmCall)).all()
    assert (row.provider, row.model, row.purpose, row.error) == ("fake", "fake-1", "t", None)
    assert json.loads(row.response) == {"ok": True}


def test_anthropic_forces_tool_and_reads_tool_input():
    seen = {}

    def create(**kw):
        seen.update(kw)
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", input={"ok": True})],
            usage=SimpleNamespace(input_tokens=7, output_tokens=3),
        )

    t = AnthropicTransport("key", "claude-x", client=SimpleNamespace(messages=SimpleNamespace(create=create)))
    raw = t.call_json(
        system="s", prompt="p", images=[b"\x89PNG\r\n\x1a\n...."], schema=Ping.model_json_schema(), name="Ping"
    )
    assert json.loads(raw.text) == {"ok": True} and raw.input_tokens == 7
    assert seen["tool_choice"] == {"type": "tool", "name": "Ping"}
    assert seen["messages"][0]["content"][0]["source"]["media_type"] == "image/png"


def test_anthropic_without_key_is_unavailable():
    with pytest.raises(LLMUnavailableError, match="ANTHROPIC_API_KEY"):
        AnthropicTransport(None, "m").call_json(system="s", prompt="p", images=[], schema={}, name="x")


def test_anthropic_api_error_is_unavailable():
    def create(**_kw):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "http://x"))

    t = AnthropicTransport("key", "m", client=SimpleNamespace(messages=SimpleNamespace(create=create)))
    with pytest.raises(LLMUnavailableError):
        t.call_json(system="s", prompt="p", images=[], schema={}, name="x")


def test_anthropic_reply_without_tool_block_is_empty_text():
    def create(**_kw):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text")], usage=SimpleNamespace(input_tokens=1, output_tokens=1)
        )

    t = AnthropicTransport("key", "m", client=SimpleNamespace(messages=SimpleNamespace(create=create)))
    assert t.call_json(system="s", prompt="p", images=[], schema={}, name="x").text == ""


def _chat_response(content, choices=None):
    return httpx.Response(
        200,
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": choices
            if choices is not None
            else [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    )


def _openai_client(handler):
    return openai.OpenAI(
        base_url="http://test/v1",
        api_key="k",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_openai_compat_falls_back_to_json_object_once():
    seen = []

    def handler(req):
        fmt = json.loads(req.content)["response_format"]["type"]
        seen.append(fmt)
        if fmt == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema not supported"}})
        return _chat_response('{"ok": true}')

    t = OpenAICompatTransport("http://test/v1", "k", "m", client=_openai_client(handler))
    raw = t.call_json(system="s", prompt="p", images=[b"\xff\xd8jpeg"], schema=Ping.model_json_schema(), name="Ping")
    assert raw.text == '{"ok": true}' and raw.input_tokens == 3
    t.call_json(system="s", prompt="p", images=[], schema=Ping.model_json_schema(), name="Ping")
    assert seen == ["json_schema", "json_object", "json_object"]


def test_openai_compat_connection_error_is_unavailable():
    def handler(req):
        raise httpx.ConnectError("refused")

    t = OpenAICompatTransport("u", "k", "m", client=_openai_client(handler))
    with pytest.raises(LLMUnavailableError):
        t.call_json(system="s", prompt="p", images=[], schema={}, name="x")


def test_openai_compat_empty_choices_gives_empty_text():
    t = OpenAICompatTransport("u", "k", "m", client=_openai_client(lambda req: _chat_response("", choices=[])))
    assert t.call_json(system="s", prompt="p", images=[], schema={}, name="x").text == ""


def test_build_transport_selects_provider(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    a = llm.build_transport(Settings(_env_file=None, llm_provider="anthropic", anthropic_model="claude-x"))
    assert isinstance(a, AnthropicTransport) and a.model == "claude-x"
    o = llm.build_transport(Settings(_env_file=None, llm_provider="openai_compatible", openai_model="qwen"))
    assert isinstance(o, OpenAICompatTransport) and o.model == "qwen"


def test_set_transport_swaps_what_later_calls_use():
    first, second = FakeTransport([{"ok": True}]), FakeTransport([{"ok": False}])
    p = LLMProvider(first)

    p.set_transport(second)

    assert p.complete_json(system="s", prompt="p", images=[], schema=Ping, purpose="t", user_id=OWNER_ID).ok is False
    assert p.model == second.model and first.calls == []


def test_with_transport_shares_the_log_sink_and_leaves_the_original_alone():
    logs = []
    original, candidate = FakeTransport(), FakeTransport([{"ok": True}])
    p = LLMProvider(original, log=logs.append)

    p.with_transport(candidate).complete_json(
        system="s", prompt="p", images=[], schema=Ping, purpose="t", user_id=OWNER_ID
    )

    assert len(logs) == 1 and p.transport is original


def test_call_log_never_stores_the_key_a_provider_echoes():
    logs = []
    t = FakeTransport([LLMUnavailableError("401 invalid key sk-LEAKED-KEY-123456")])
    t.api_key = "sk-LEAKED-KEY-123456"

    with pytest.raises(LLMUnavailableError):
        LLMProvider(t, log=logs.append).complete_json(
            system="s", prompt="p", images=[], schema=Ping, purpose="t", user_id=OWNER_ID
        )

    assert "sk-LEAKED-KEY-123456" not in logs[0].error and "401 invalid key ***" in logs[0].error


def test_anthropic_lists_model_ids():
    seen = {}

    def list_models(**kw):
        seen.update(kw)
        return [SimpleNamespace(id="model-b"), SimpleNamespace(id="model-a")]

    t = AnthropicTransport("key", "m", client=SimpleNamespace(models=SimpleNamespace(list=list_models)))

    assert t.list_models() == ["model-b", "model-a"]  # provider order: newest first


def test_anthropic_list_models_without_key_is_unavailable():
    with pytest.raises(LLMUnavailableError):
        AnthropicTransport(None, "m").list_models()


def test_openai_compat_lists_model_ids_sorted():
    def handler(req):
        assert req.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "zeta", "object": "model", "created": 0, "owned_by": "x"},
                    {"id": "alpha", "object": "model", "created": 0, "owned_by": "x"},
                ],
            },
        )

    client = openai.OpenAI(
        base_url="http://test/v1",
        api_key="k",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert OpenAICompatTransport("http://test/v1", "k", "m", client=client).list_models() == ["alpha", "zeta"]


def test_openai_compat_list_models_error_is_unavailable():
    client = openai.OpenAI(
        base_url="http://test/v1",
        api_key="k",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))),
    )

    with pytest.raises(LLMUnavailableError):
        OpenAICompatTransport("http://test/v1", "k", "m", client=client).list_models()


def test_call_log_describes_the_transport_that_made_the_call_even_if_it_is_swapped_meanwhile():
    logs = []
    new = FakeTransport()
    new.api_key, new.model = "sk-NEW-KEY-000000", "new-model"

    class Swapping(FakeTransport):
        api_key = "sk-OLD-KEY-111111"

        def call_json(self, **kw):
            provider.set_transport(new)  # the user saves new settings while this call is in flight
            raise LLMUnavailableError("401 invalid key sk-OLD-KEY-111111")

    provider = LLMProvider(Swapping(), log=logs.append)

    with pytest.raises(LLMUnavailableError):
        provider.complete_json(system="s", prompt="p", images=[], schema=Ping, purpose="t", user_id=OWNER_ID)

    assert "sk-OLD-KEY-111111" not in logs[0].error
    assert (logs[0].provider, logs[0].model) == ("fake", "fake-1")


def test_missing_key_message_points_to_settings():
    with pytest.raises(LLMUnavailableError, match="Settings"):
        AnthropicTransport(None, "m").call_json(system="s", prompt="p", images=[], schema={}, name="x")


def _status_error(cls, status, body):
    response = httpx.Response(status, request=httpx.Request("POST", "http://test/v1/x"), json=body)
    return cls(f"Error code: {status} - {body}", response=response, body=body.get("error", body))


def test_openai_errors_reach_people_as_the_providers_own_sentence():
    def refuse(**kw):
        raise _status_error(
            openai.AuthenticationError,
            401,
            {"error": {"message": "Incorrect API key provided.", "code": "invalid_api_key"}},
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=refuse)), models=SimpleNamespace(list=refuse)
    )
    t = OpenAICompatTransport("u", "k", "m", client=client)

    with pytest.raises(LLMUnavailableError) as e:
        t.call_json(system="s", prompt="p", images=[], schema={}, name="x")

    assert str(e.value) == "Incorrect API key provided. (error 401)"
    with pytest.raises(LLMUnavailableError, match=r"^Incorrect API key provided\. \(error 401\)$"):
        t.list_models()


def test_anthropic_errors_reach_people_as_the_providers_own_sentence():
    def refuse(**kw):
        body = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
        response = httpx.Response(401, request=httpx.Request("POST", "http://test/v1/messages"), json=body)
        raise anthropic.AuthenticationError("Error code: 401 - {...}", response=response, body=body)

    t = AnthropicTransport("key", "m", client=SimpleNamespace(messages=SimpleNamespace(create=refuse)))

    with pytest.raises(LLMUnavailableError) as e:
        t.call_json(system="s", prompt="p", images=[], schema={}, name="x")

    assert str(e.value) == "invalid x-api-key (error 401)"


def test_errors_without_a_readable_body_keep_their_text():
    from kyapehnu.agent.llm.base import provider_message

    assert provider_message(RuntimeError("Connection error.")) == "Connection error."


# --- the seams the other work packages fill: a keyword-only user_id and the Valkey client ----------------------------


def test_complete_json_accepts_a_user_id_by_keyword_and_logs_as_before():
    p, logs = _provider([{"ok": True}])
    assert _ask(p, user_id=7) == Ping(ok=True)
    assert len(logs) == 1 and logs[0].error is None


def test_complete_json_takes_the_user_id_by_keyword_only():
    p, _ = _provider([{"ok": True}])
    with pytest.raises(TypeError):
        p.complete_json("s", "p", [], Ping, "t", None, 7)  # type: ignore[misc]


def test_a_call_without_the_user_id_is_refused_before_it_reaches_the_provider():
    transport = FakeTransport([{"ok": True}])
    transport.edits.append(b"PNG")
    p = LLMProvider(transport)

    with pytest.raises(TypeError, match="user_id"):
        p.complete_json(system="s", prompt="p", images=[], schema=Ping, purpose="t")
    with pytest.raises(TypeError, match="user_id"):
        p.edit_image(image=b"x", prompt="p", model="m", purpose="t")

    assert transport.edits == [b"PNG"]


def test_chat_and_edit_image_accept_a_user_id_by_keyword():
    t = FakeTransport(chat_replies=[ChatResult("hi", (), 1, 1)])
    t.edits.append(b"PNG")
    p = LLMProvider(t)
    reply = p.chat(system="s", messages=[], tools=[], label="l", first_call=True, user_id=7)
    assert reply.text == "hi"
    assert p.edit_image(image=b"x", prompt="p", model="m", purpose="t", user_id=7) == b"PNG"


def test_the_valkey_client_is_kept_and_carried_to_a_trial_provider():
    kv = object()
    p = LLMProvider(FakeTransport([]), kv=kv)  # type: ignore[arg-type]
    assert p.kv is kv
    assert p.with_transport(FakeTransport([])).kv is kv
    assert LLMProvider(FakeTransport([])).kv is None


def test_the_app_gives_its_provider_the_valkey_client_it_was_built_with(app, kv):
    assert app.state.ctx.provider.kv is kv


def test_the_turns_dependency_is_typed_through_the_one_alias_in_agent_turns():
    import typing

    from kyapehnu.agent.turns import Turns
    from kyapehnu.core.deps import TurnsDep, get_turns

    assert typing.get_type_hints(get_turns)["return"] is Turns
    assert typing.get_args(TurnsDep)[0] is Turns
