"""A scripted stylist for a manual browser run of the Chat page (docs/manual-checks/phase3-chat.md). Not a test.

    cd backend
    KYAPEHNU_DATA_DIR=/some/scratch/dir uv run kyapehnu seed
    KYAPEHNU_DATA_DIR=/some/scratch/dir uv run python -m tests.browser_stand_in --port 8770

Then open http://localhost:8770/chat. The app runs on the real code with a stand-in provider (no key, no network: the
weather is the scripted Open-Meteo of the tests) and the built frontend (`npm run build` in frontend/ first).

What to type (the stand-in reads the last message, case-insensitively):
  dinner ...   create_event (next Saturday 20:00, Khan Market, smart casual, with Priya), generate_outfits, reply
  wear ...     proposes log_wear of the seed items 7, 10, 21 (Confirm / Cancel card)
  laundry ...  proposes update_item_status of item 7 to laundry
  markup ...   a reply holding <img onerror> and a markdown link (must show as literal text)
  fail ...     the provider fails (a plain failure notice; send again to retry)
  anything else: a short help text
"""

import argparse
import json
import os
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from kyapehnu.agent.llm.base import (
    ChatResult,
    LLMUnavailableError,
    Msg,
    RawResult,
    ToolCallRequest,
    ToolResultMsg,
    ToolSpec,
    UserMsg,
)
from kyapehnu.core.app import create_app
from kyapehnu.core.config import Settings
from tests.fake_openmeteo import FakeOpenMeteo

WORN = [
    7,
    10,
    21,
]  # the seed items "Maroon striped shirt", a bottom and a shoe: the first suggestion of the seed wardrobe
HELP = "I am a stand-in stylist. Try: dinner on Saturday, wear, laundry, markup, fail."
PICK = {"picks": [{"candidate_number": 1, "reason": "A calm, balanced look.", "swap_slot": None, "swap_item_id": None}]}


def _saturday() -> str:
    today = date.today()
    day = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    return datetime.combine(day, datetime.min.time()).replace(hour=20).isoformat()


class StylistStandIn:
    provider = "stand-in"
    model = "stand-in-1"

    def list_models(self) -> list[str]:
        return [self.model]

    def call_json(
        self, *, system: str, prompt: str, images: list[bytes], schema: dict[str, Any], name: str
    ) -> RawResult:
        return RawResult(json.dumps(PICK), 10, 5)

    def call_chat(self, *, system: str, messages: Sequence[Msg], tools: Sequence[ToolSpec]) -> ChatResult:
        last = messages[-1]
        if isinstance(last, ToolResultMsg):
            return self._after(last)
        assert isinstance(last, UserMsg)
        text = last.text.lower()
        if "fail" in text:
            raise LLMUnavailableError("The stand-in provider is set to fail (type anything else to recover).")
        if "markup" in text:
            return _say("<img src=x onerror=alert(1)> and [a link](http://example.invalid) and **bold**")
        if "dinner" in text:
            event = {
                "type": "party",
                "title": "Dinner with Priya",
                "starts_at": _saturday(),
                "place": "Khan Market",
                "dress_code": "smart casual",
                "audience_tags": ["Priya"],
            }
            return _ask(("create_event", event))
        if "wear" in text:
            return _ask(("log_wear", {"item_ids": WORN}))
        if "laundry" in text:
            return _ask(("update_item_status", {"item_id": WORN[0], "status": "laundry"}))
        return _say(HELP)

    def _after(self, result: ToolResultMsg) -> ChatResult:
        if result.name == "create_event" and not result.is_error:
            return _ask(("generate_outfits", {"event_id": json.loads(result.content)["id"]}))
        if result.name == "generate_outfits":
            return _say("Here are looks for Saturday. The weather note is on each card.")
        if result.name in ("log_wear", "update_item_status"):
            return _say("I have put that to you as a card: press Confirm to make the change, or Cancel.")
        return _say("Done.")


def _say(text: str) -> ChatResult:
    return ChatResult(text, (), 10, 5)


def _ask(*calls: tuple[str, dict[str, Any]]) -> ChatResult:
    return ChatResult("", tuple(ToolCallRequest(f"c{n}", name, args) for n, (name, args) in enumerate(calls, 1)), 10, 5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    data_dir = os.environ.get("KYAPEHNU_DATA_DIR")
    if not data_dir:
        raise SystemExit("set KYAPEHNU_DATA_DIR to a scratch folder (never your real data)")
    # A made-up key only so that the app counts the provider as configured; the transport below ignores it.
    settings = Settings(
        _env_file=None,
        data_dir=Path(data_dir),
        config_file=Path(data_dir) / "no-config.yaml",
        llm_provider="anthropic",
        anthropic_api_key="stand-in-not-a-real-key",
    )
    app = create_app(
        settings=settings, transport=StylistStandIn(), weather_transport=httpx.MockTransport(FakeOpenMeteo())
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
