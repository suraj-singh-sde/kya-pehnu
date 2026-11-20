"""Bounds that both the wardrobe routes and the chat tools enforce, so neither domain imports the other for them."""

from typing import Final

MAX_WEARS: Final = 100
MIN_DAYS: Final = 1
MAX_DAYS: Final = 3650
DEFAULT_DAYS: Final = 90
MAX_REASON_CHARS: Final = 500
MAX_INCLUDE: Final = 6
MAX_EXCLUDE: Final = 50
