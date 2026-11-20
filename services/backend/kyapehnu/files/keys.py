"""The picture address grammar: images/<user id>/<kind>/<name>. Every store checks a key here before it touches
storage, which is the one traversal guard for the disk and for S3."""

import mimetypes
import re
from typing import Final

from kyapehnu.files.errors import InvalidKeyError

# StaticFiles and S3 uploads ask the mimetypes registry; Windows has no entry for .webp and would answer octet-stream.
mimetypes.add_type("image/webp", ".webp")

KINDS: Final = ("original", "cutout", "thumb", "collage", "mannequin")
# No dot-only name, no separator, so a file with such a name is always servable.
IMAGE_NAME: Final = r"[A-Za-z0-9_-]{1,200}\.(?:jpg|jpeg|png|webp|heic|heif)"
KIND_ALTERNATIVES: Final = "|".join(KINDS)
ROOT: Final = "images/"
KEY_PATTERN: Final = re.compile(rf"{ROOT}([1-9][0-9]*)/({KIND_ALTERNATIVES})/({IMAGE_NAME})")
KIND_PATTERN: Final = f"^({KIND_ALTERNATIVES})$"
NAME_PATTERN: Final = f"^{IMAGE_NAME}$"


def check(key: str) -> str:
    """The key itself, or InvalidKeyError."""
    if not KEY_PATTERN.fullmatch(key):
        raise InvalidKeyError("not a picture key")
    return key


def key(user_id: int, kind: str, name: str) -> str:
    return check(f"{ROOT}{user_id}/{kind}/{name}")


def prefix(user_id: int | None = None, kind: str | None = None) -> str:
    """`images/`, `images/7/` or `images/7/thumb/`: what `FileStore.list` narrows to."""
    if user_id is None:
        if kind is not None:
            raise InvalidKeyError("a kind needs a user")
        return ROOT
    return f"{ROOT}{user_id}/" if kind is None else f"{ROOT}{user_id}/{kind}/"


def parse_key(address: str) -> tuple[int, str, str] | None:
    """(user id, kind, name), or None when the text is not a picture key."""
    match = KEY_PATTERN.fullmatch(address)
    return None if match is None else (int(match[1]), match[2], match[3])
