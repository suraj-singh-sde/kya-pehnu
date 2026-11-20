"""Can this model actually read a photo? Provider model lists include text-only models, and nothing marks them."""

import io
import random
from typing import Final

from PIL import Image
from pydantic import BaseModel

from kyapehnu.agent.llm.base import LLMProvider

COLORS: Final[dict[str, tuple[int, int, int]]] = {
    "red": (220, 30, 30),
    "green": (30, 160, 60),
    "blue": (30, 60, 220),
    "yellow": (240, 220, 40),
    "black": (0, 0, 0),
    "white": (255, 255, 255),
}
PROBE_SIZE: Final = (256, 128)
CANNOT_SEE: Final = "This model could not read the test photo. Pick a model that supports images."
SYSTEM: Final = "You describe images precisely."
PROMPT: Final = (
    "The image has two halves, each filled with one colour. "
    f"Answer with the colour of the left half and of the right half, using only these words: {', '.join(COLORS)}."
)


class VisionCheckError(Exception):
    pass


class ColorHalves(BaseModel):
    left: str
    right: str


def make_probe(rng: random.Random | None = None) -> tuple[bytes, str, str]:
    """A PNG with two random, different colours, and their names. A model guessing blind passes ~3% of the time."""
    left, right = (rng or random.Random()).sample(sorted(COLORS), 2)
    width, height = PROBE_SIZE
    im = Image.new("RGB", PROBE_SIZE, COLORS[left])
    im.paste(COLORS[right], (width // 2, 0, width, height))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue(), left, right


def check_vision(provider: LLMProvider, user_id: int) -> None:
    """Raise VisionCheckError when the model answers but cannot see; provider errors (LLMError) pass through."""
    png, left, right = make_probe()
    answer = provider.complete_json(
        system=SYSTEM, prompt=PROMPT, images=[png], schema=ColorHalves, purpose="vision_check", user_id=user_id
    )
    if left not in answer.left.lower() or right not in answer.right.lower():
        raise VisionCheckError(CANNOT_SEE)
