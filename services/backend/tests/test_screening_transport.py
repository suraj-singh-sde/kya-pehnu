"""The decisions endpoint through the OpenAI-compatible transport: what it sends and what it makes of the reply."""

import base64
from typing import Any

import pytest

from kyapehnu.agent.llm.base import LLMOutputError, LLMUnavailableError
from kyapehnu.agent.llm.openai_compat import OpenAICompatTransport
from tests.sdk_fakes import OLLAMA_URL, sdk_error

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
QUESTION = {"type": "choice", "name": "shows", "instructions": "What?", "choices": [{"value": "a", "description": "A"}]}


class _Posting:
    """The one SDK method the transport uses for decisions: `client.post(path, body=..., cast_to=...)`."""

    def __init__(self, *script: Any) -> None:
        self.script, self.calls = list(script), []

    def with_options(self, **options: Any) -> "_Posting":
        self.options = options
        return self

    def post(self, path: str, **kw: Any) -> Any:
        self.calls.append({"path": path, **kw})
        reply = self.script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _transport(*script: Any) -> tuple[OpenAICompatTransport, _Posting]:
    client = _Posting(*script)
    return OpenAICompatTransport(OLLAMA_URL, "k", "m", client=client), client


def test_decide_image_posts_the_picture_and_the_questions_and_returns_the_answers():
    answer = {"type": "choice", "name": "shows", "choice": "a", "confidence": 0.9}
    t, client = _transport({"model": "gpt-6-luna", "answers": [answer, "junk"], "usage": {}})
    assert t.decide_image(image=PNG, text="Look.", questions=[QUESTION]) == [answer]
    [call] = client.calls
    assert call["path"] == "/decisions" and call["body"]["model"] == "gpt-6-luna"
    [message] = call["body"]["input"]
    assert message["role"] == "user" and message["content"][0] == {"type": "input_text", "text": "Look."}
    assert message["content"][1]["image_url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert call["body"]["questions"] == [QUESTION]


@pytest.mark.parametrize("reply", [{"answers": None}, {"nope": 1}, "text", None])
def test_a_reply_without_answers_is_an_output_error(reply):
    t, _ = _transport(reply)
    with pytest.raises(LLMOutputError):
        t.decide_image(image=PNG, text="Look.", questions=[QUESTION])


def test_an_api_error_is_unavailable():
    t, _ = _transport(sdk_error("openai", "APIStatusError", 503, "down"))
    with pytest.raises(LLMUnavailableError):
        t.decide_image(image=PNG, text="Look.", questions=[QUESTION])


def test_max_retries_reaches_the_sdk_client_only_when_given():
    answer = {"type": "choice", "name": "shows", "choice": "a", "confidence": 0.9}
    t, client = _transport({"answers": [answer]}, {"answers": [answer]})

    t.decide_image(image=PNG, text="Look.", questions=[QUESTION])
    assert not hasattr(client, "options")
    t.decide_image(image=PNG, text="Look.", questions=[QUESTION], max_retries=0)
    assert client.options == {"max_retries": 0}
