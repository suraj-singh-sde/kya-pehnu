"""Helpers of the files-domain tests: small pictures as bytes, and the shape of the answers."""

import base64
import hashlib
import io

from PIL import Image

FORMATS = {".png": "PNG", ".webp": "WEBP", ".jpg": "JPEG", ".jpeg": "JPEG"}
PROBLEM_KEYS = ("detail", "code")  # the parts of an error body that do not change from one request to the next


def picture(ext: str = ".png", rgb: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    """A small real picture in the format of `ext`."""
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), rgb).save(buf, FORMATS[ext])
    return buf.getvalue()


def sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def problem(response) -> tuple:
    """What a 4xx/5xx answer says, without the per-request id: status, detail, code, content type."""
    body = response.json()
    return (response.status_code, *(body[k] for k in PROBLEM_KEYS), response.headers["content-type"])


class OneWay:
    """A stream that can only be read once from the front (like a zip member): no seek, no tell."""

    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._data.read(size)


class Exploding:
    """A stream that gives some bytes and then fails like a dropped upload."""

    def __init__(self, first: bytes = b"x" * 1024) -> None:
        self._first: bytes | None = first

    def read(self, size: int = -1) -> bytes:
        if self._first is None:
            raise OSError("connection reset (injected)")
        data, self._first = self._first, None
        return data
