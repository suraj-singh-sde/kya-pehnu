from typing import Any, Final, Literal

from pydantic import BaseModel, field_validator
from sqlalchemy import JSON, Column, String
from sqlmodel import Field, SQLModel

from kyapehnu.core.errors import InvalidInputError

# The almirah's finishes (frontend presets) and the categories a shelf may hold. The categories mirror
# kyapehnu.wardrobe.models.Category (a test keeps them equal); the user domain does not import the wardrobe one.
THEMES: Final = ("teal", "walnut", "oak", "ivory", "navy", "rose")
SHELF_CATEGORIES: Final = frozenset({"top", "bottom", "dress", "outerwear", "footwear", "ethnic", "accessory", "bag"})
MAX_SHELVES: Final = 12
Theme = Literal["teal", "walnut", "oak", "ivory", "navy", "rose"]


class Shelf(BaseModel):
    """One compartment of the almirah: its label, how it shows, how wide it is, and which categories it holds.

    `match` narrows it to the pieces of those categories the named word list picks (the page knows the words);
    the rest of each category stays on the other shelves."""

    name: str = Field(min_length=1, max_length=30)
    kind: Literal["rail", "shelf", "drawer", "locker"]
    width: Literal["full", "half"]
    cats: list[str] = Field(min_length=1)
    match: Literal["caps_glasses", "sarees", "jewellery"] | None = None

    @field_validator("cats")
    @classmethod
    def _known_categories(cls, cats: list[str]) -> list[str]:
        unknown = sorted(set(cats) - SHELF_CATEGORIES)
        if unknown:
            raise InvalidInputError(f"unknown categories: {', '.join(unknown)}")
        return cats


class ShelfLayout(BaseModel):
    """The whole almirah as a list; the page lays shelves out in this order, two halves sharing a row."""

    shelves: list[Shelf] = Field(min_length=1, max_length=MAX_SHELVES)


class UserProfileBase(SQLModel):
    body_notes: str = ""
    skin_tone: str = ""
    style_prefs: str = ""
    colors_loved: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    colors_avoided: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    fit_prefs: str = ""
    budget_min: int = 1000
    budget_max: int = 5000
    home_city: str = "Gurugram"
    height_cm: int | None = Field(default=None, ge=100, le=250)
    weight_kg: int | None = Field(default=None, ge=25, le=300)
    # Asked once, at onboarding. "" means the person has not been through it yet; the almirah's compartments and
    # the mannequin follow the gender.
    name: str = Field(default="", max_length=60)
    age: int | None = Field(default=None, ge=10, le=120)
    gender: Literal["", "male", "female"] = Field(
        default="", sa_column=Column(String(8), nullable=False, server_default="")
    )
    # The almirah's finish (a preset name) and its shelves (null: the default layout for the gender).
    theme: Theme = Field(default="teal", sa_column=Column(String(16), nullable=False, server_default="teal"))
    layout: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))

    @field_validator("layout")
    @classmethod
    def _valid_layout(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return ShelfLayout.model_validate(value).model_dump(exclude_none=True)


class UserProfile(UserProfileBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True)
