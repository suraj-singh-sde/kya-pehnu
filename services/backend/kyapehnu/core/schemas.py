import re
import unicodedata
from datetime import date
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, model_validator

from kyapehnu.core.errors import InvalidInputError
from kyapehnu.wardrobe.models import Category, Pattern, Season

Rating = Annotated[int, Field(ge=1, le=5)]
TAG_FIELDS = (
    "name",
    "category",
    "subcategory",
    "colors",
    "pattern",
    "fabric",
    "formality",
    "warmth",
    "seasons",
    "fit",
    "tags",
    "description",
)
MAX_COLORS = 3
MAX_ITEM_IDS: Final = 10
MAX_BULK_ITEM_IDS: Final = 200
MAX_ROW_ID: Final = 2**63 - 1  # SQLite INTEGER: a larger id would be a 500 from the driver, not a 422

RowId = Annotated[int, Field(gt=0, le=MAX_ROW_ID)]


class Color(BaseModel):
    hex: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    name: str
    role: Literal["primary", "secondary"] = "secondary"


def _fix_roles(colors: list[Color] | None) -> None:
    for i, c in enumerate(colors or []):
        c.role = "primary" if i == 0 else "secondary"


class GarmentTags(BaseModel):
    name: str
    category: Category
    subcategory: str = ""
    colors: list[Color] = Field(min_length=1, max_length=MAX_COLORS)
    pattern: Pattern
    fabric: str | None = None
    formality: Rating
    warmth: Rating
    seasons: list[Season] = []
    fit: str | None = None
    tags: list[str] = []
    description: str = ""  # a stylist's few sentences about the piece: cut, details, how it wears

    @model_validator(mode="after")
    def _roles(self) -> Self:
        _fix_roles(self.colors)
        return self


class TagsPatch(BaseModel):
    name: str | None = None
    category: Category | None = None
    subcategory: str | None = None
    colors: list[Color] | None = Field(default=None, min_length=1, max_length=MAX_COLORS)
    pattern: Pattern | None = None
    fabric: str | None = None
    formality: Rating | None = None
    warmth: Rating | None = None
    seasons: list[Season] | None = None
    fit: str | None = None
    tags: list[str] | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _roles(self) -> Self:
        _fix_roles(self.colors)
        return self


class ItemPatch(TagsPatch):
    brand: str | None = None
    purchase_price: float | None = Field(default=None, ge=0)
    purchase_date: date | None = None
    notes: str | None = None


# Cleaned text for request models. BeforeValidator runs first, so Field limits apply to the cleaned value.
LINE_BREAKS = re.compile("[\t\r\n\x0b\x0c\x85\u2028\u2029]+")


def _drop_controls(text: str, keep: str = "") -> str:
    return "".join(c for c in text if c in keep or not unicodedata.category(c).startswith("C"))


def clean_line(v: object) -> object:
    """Strip control and format characters (zero-width, bidi overrides), turn line breaks into spaces, trim."""
    if not isinstance(v, str):
        return v
    return _drop_controls(LINE_BREAKS.sub(" ", v)).strip()


def clean_para(v: object) -> object:
    """Like clean_line, but a paragraph keeps its newlines."""
    if not isinstance(v, str):
        return v
    return _drop_controls(v, keep="\n").strip()


def _distinct(ids: list[int]) -> list[int]:
    if len(set(ids)) != len(ids):
        raise InvalidInputError("item ids must be distinct")
    return ids


DistinctIds = Annotated[list[RowId], AfterValidator(_distinct)]
Line = Annotated[str, BeforeValidator(clean_line)]
Para = Annotated[str, BeforeValidator(clean_para)]
ItemIds = Annotated[list[RowId], Field(min_length=1, max_length=MAX_ITEM_IDS), AfterValidator(_distinct)]
BulkItemIds = Annotated[list[RowId], Field(min_length=1, max_length=MAX_BULK_ITEM_IDS), AfterValidator(_distinct)]
