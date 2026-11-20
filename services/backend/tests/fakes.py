import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from kyapehnu.agent.llm.base import RawResult
from kyapehnu.wardrobe.models import Item, Outfit, WearLog


class FakeTransport:
    provider = "fake"
    model = "fake-1"

    def __init__(self, replies=None, models=None, chat_replies=None):
        self.replies = list(replies or [])
        self.models = models
        # Scripted answers of call_chat: a ChatResult, an Exception, or a callable run first (it can block on a
        # threading.Event and then returns the ChatResult or Exception to use).
        self.chat_replies: list = list(chat_replies or [])
        self.chat_calls: list[dict] = []  # {"system", "messages", "tools"} per request, as sent
        self.edits: list = []  # scripted results for edit_image: PNG bytes or an exception
        self.edit_calls: list[dict] = []
        # Scripted answers of decide_image (a list of answer dicts, or an exception); unscripted, a photo is a garment,
        # so every upload test passes the screening without saying so.
        self.decisions: list = []
        self.decide_calls: list[dict] = []
        self.calls: list[dict] = []

    def decide_image(self, *, image, text, questions, max_retries=None):
        self.decide_calls.append({"image": image, "text": text, "questions": questions, "max_retries": max_retries})
        if not self.decisions:  # unscripted: a garment, a good outfit, no clash
            default = {"shows": "garment", "grade": "good", "clash": "no"}
            return [
                {"type": "choice", "name": q["name"], "choice": default.get(q["name"], "garment"), "confidence": 1.0}
                for q in questions
            ]
        reply = self.decisions.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def edit_image(self, *, image, prompt, model):
        self.edit_calls.append({"image": image, "prompt": prompt, "model": model})
        if not self.edits:
            raise AssertionError("unexpected image generation")
        reply = self.edits.pop(0)
        if callable(reply):  # something happens while the picture is being made
            reply = reply()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def call_chat(self, *, system, messages, tools):
        self.chat_calls.append({"system": system, "messages": list(messages), "tools": list(tools)})
        if not self.chat_replies:
            raise AssertionError("unexpected chat call")
        reply = self.chat_replies.pop(0)
        if callable(reply):  # something happens while the model "thinks"
            reply = reply()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def list_models(self):
        if isinstance(self.models, Exception):
            raise self.models
        return list(self.models or [])

    def call_json(self, *, system, prompt, images, schema, name, max_retries=None):
        self.calls.append(
            {"system": system, "prompt": prompt, "images": images, "name": name, "max_retries": max_retries}
        )
        if not self.replies:
            raise AssertionError(f"unexpected LLM call ({name})")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return RawResult(reply if isinstance(reply, str) else json.dumps(reply), 10, 5)


def garment(bbox=(0.1, 0.1, 0.9, 0.9), **tags):
    base = {
        "name": "White shirt",
        "category": "top",
        "subcategory": "shirt",
        "colors": [{"hex": "#ffffff", "name": "white"}],
        "pattern": "solid",
        "fabric": "cotton",
        "formality": 3,
        "warmth": 2,
        "seasons": ["summer"],
        "fit": "regular",
        "tags": [],
    }
    base.update(tags)
    return {"bbox": list(bbox), "tags": base}


def analysis(*garments, photo_type="flat_lay"):
    return {"photo_type": photo_type, "garments": list(garments)}


OWNER_ID = 1  # the account the tests written before accounts act as: "sub-a", the person of the `client` fixture


def ensure_owner(s: Session) -> int:
    """User `OWNER_ID`, created when missing, so rows that name it satisfy the foreign key."""
    now = "2026-01-01 00:00:00"
    s.connection().execute(
        text(
            'INSERT OR IGNORE INTO "user" (id, google_sub, email, display_name, is_admin, created_at, last_sign_in_at) '
            "VALUES (:id, 'sub-a', 'sub-a@example.com', 'sub-a', 1, :now, :now)"
        ),
        {"id": OWNER_ID, "now": now},
    )
    return OWNER_ID


def stored_files(data_dir: Path, kind: str = "original", user_id: int = OWNER_ID) -> list[str]:
    """Names of the files under data/images/<user_id>/<kind>."""
    folder = data_dir / "images" / str(user_id) / kind
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def add_item(s: Session, **fields: Any) -> Item:
    """Commit an available top; any field can be overridden, an explicit empty or None value included."""
    item = Item(**{"user_id": ensure_owner(s), "name": "Item", "category": "top", **fields})
    s.add(item)
    s.commit()
    s.refresh(item)
    return item


def add_wear(
    s: Session, item_ids: Sequence[int], worn_on: date, audience: Sequence[str] = (), outfit: bool = True
) -> WearLog:
    """Commit a wear of these items. With outfit=True it is linked to the saved outfit of the same set of items."""
    key = ",".join(str(i) for i in sorted(item_ids))
    saved = s.exec(select(Outfit).where(Outfit.item_key == key)).first() if outfit else None
    if outfit and saved is None:
        saved = Outfit(user_id=ensure_owner(s), item_ids=list(item_ids), item_key=key, name=f"Outfit {key}")
        s.add(saved)
        s.flush()
    wear = WearLog(
        user_id=ensure_owner(s),
        outfit_id=saved.id if saved is not None else None,
        item_ids=list(item_ids),
        worn_on=worn_on,
        audience_tags=list(audience),
    )
    s.add(wear)
    s.commit()
    s.refresh(wear)
    return wear
