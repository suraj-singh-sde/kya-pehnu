import io
import random

import pytest
from PIL import Image

from kyapehnu.agent.llm import vision_check
from kyapehnu.agent.llm.base import LLMOutputError, LLMProvider, LLMUnavailableError
from tests.fakes import FakeTransport


def test_probe_is_two_different_known_colours():
    png, left, right = vision_check.make_probe(random.Random(3))
    im = Image.open(io.BytesIO(png)).convert("RGB")

    assert left != right and {left, right} <= set(vision_check.COLORS)
    assert im.getpixel((10, 10)) == vision_check.COLORS[left]
    assert im.getpixel((im.width - 10, 10)) == vision_check.COLORS[right]


def test_probes_vary():
    assert len({vision_check.make_probe(random.Random(i))[1:] for i in range(30)}) > 5


@pytest.fixture
def fixed_probe(monkeypatch):
    monkeypatch.setattr(vision_check, "make_probe", lambda rng=None: (b"\x89PNG-probe", "red", "blue"))


def test_correct_answer_passes_and_sends_the_image(fixed_probe):
    t = FakeTransport([{"left": "Red", "right": "dark blue"}])

    vision_check.check_vision(LLMProvider(t), 1)

    assert t.calls[0]["images"] == [b"\x89PNG-probe"]


@pytest.mark.parametrize(
    "answer", [{"left": "blue", "right": "red"}, {"left": "red", "right": "green"}, {"left": "", "right": ""}]
)
def test_wrong_colours_fail_with_a_plain_message(fixed_probe, answer):
    with pytest.raises(vision_check.VisionCheckError, match="could not read the test photo"):
        vision_check.check_vision(LLMProvider(FakeTransport([answer])), 1)


def test_provider_errors_pass_through(fixed_probe):
    with pytest.raises(LLMUnavailableError):
        vision_check.check_vision(LLMProvider(FakeTransport([LLMUnavailableError("401")])), 1)
    with pytest.raises(LLMOutputError):
        vision_check.check_vision(LLMProvider(FakeTransport(["nope", "still nope"])), 1)


def test_the_check_is_logged_as_its_own_purpose(fixed_probe):
    logged = []

    vision_check.check_vision(LLMProvider(FakeTransport([{"left": "red", "right": "blue"}]), log=logged.append), 7)

    assert [(c.purpose, c.user_id) for c in logged] == [("vision_check", 7)]
