from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import JSON, CheckConstraint, Column, Connection, DateTime, Index, UniqueConstraint, event, false, text
from sqlalchemy.orm import Mapper
from sqlmodel import Field, SQLModel

from kyapehnu.core.db import utcnow


class Category(StrEnum):
    top = "top"
    bottom = "bottom"
    dress = "dress"
    outerwear = "outerwear"
    footwear = "footwear"
    ethnic = "ethnic"
    accessory = "accessory"
    bag = "bag"


class Pattern(StrEnum):
    solid = "solid"
    stripe = "stripe"
    check = "check"
    print = "print"
    floral = "floral"
    embroidered = "embroidered"
    other = "other"


class Season(StrEnum):
    summer = "summer"
    monsoon = "monsoon"
    autumn = "autumn"
    winter = "winter"
    spring = "spring"


class ItemStatus(StrEnum):
    available = "available"
    laundry = "laundry"
    tailor = "tailor"
    packed = "packed"
    retired = "retired"


class PhotoType(StrEnum):
    flat_lay = "flat_lay"
    hanger = "hanger"
    worn = "worn"
    unknown = "unknown"


class PhotoStatus(StrEnum):
    pending = "pending"
    done = "done"
    failed = "failed"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class PieceState(StrEnum):
    pending_review = "pending_review"
    accepted = "accepted"
    discarded = "discarded"
    merged = "merged"
    error = "error"
    rejected = "rejected"  # not clothing, or innerwear: the message says which; only Remove makes sense


class IngestJob(SQLModel, table=True):
    __table_args__ = (Index("ix_ingestjob_user_id_id", "user_id", "id"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    status: str = JobStatus.queued
    source: str  # upload | folder
    total: int = 0
    processed: int = 0
    failed: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Photo(SQLModel, table=True):
    __table_args__ = (Index("ix_photo_user_id", "user_id"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    job_id: int | None = Field(default=None, foreign_key="ingestjob.id", index=True)
    original_path: str
    normalized_path: str | None = None
    phash: str | None = None
    photo_type: str = PhotoType.unknown
    status: str = PhotoStatus.pending
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Item(SQLModel, table=True):
    __table_args__ = (
        CheckConstraint("formality BETWEEN 1 AND 5", name="ck_item_formality"),
        CheckConstraint("warmth BETWEEN 1 AND 5", name="ck_item_warmth"),
        Index("ix_item_user_id_status", "user_id", "status"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    photo_id: int | None = Field(default=None, foreign_key="photo.id")
    bbox: list[float] | None = Field(default=None, sa_column=Column(JSON))
    name: str
    category: str = Field(index=True)
    subcategory: str = ""
    colors: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    pattern: str = Pattern.solid
    fabric: str | None = None
    formality: int = 3
    warmth: int = 3
    seasons: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    fit: str | None = None
    brand: str | None = None
    purchase_price: float | None = None
    purchase_date: date | None = None
    status: str = Field(default=ItemStatus.available, index=True)
    cutout_path: str | None = None
    thumb_path: str | None = None
    alt_cutout_path: str | None = None  # the other picture of this garment (see kyapehnu.wardrobe.pictures)
    alt_thumb_path: str | None = None
    picture_generated: bool = Field(default=False, sa_column_kwargs={"server_default": false()})
    phash: str | None = None
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    description: str = Field(default="", sa_column_kwargs={"server_default": ""})
    notes: str = ""
    ai_tags: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ItemCorrection(SQLModel, table=True):
    __table_args__ = (Index("ix_itemcorrection_user_id", "user_id"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    item_id: int = Field(foreign_key="item.id", index=True)
    field: str
    ai_value: Any = Field(default=None, sa_column=Column(JSON))
    user_value: Any = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class IngestPiece(SQLModel, table=True):
    __table_args__ = (Index("ix_ingestpiece_user_id_state", "user_id", "state"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    job_id: int = Field(foreign_key="ingestjob.id", index=True)
    photo_id: int = Field(foreign_key="photo.id", index=True)
    bbox: list[float] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    cutout_path: str | None = None
    thumb_path: str | None = None
    alt_cutout_path: str | None = None  # the other picture of this garment (see kyapehnu.wardrobe.pictures)
    alt_thumb_path: str | None = None
    picture_generated: bool = Field(default=False, sa_column_kwargs={"server_default": false()})
    phash: str | None = None
    proposed_tags: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    duplicate_of_item_id: int | None = Field(default=None, foreign_key="item.id")
    dup_reason: str | None = None
    state: str = Field(default=PieceState.pending_review, index=True)
    error: str | None = None
    item_id: int | None = Field(default=None, foreign_key="item.id")
    created_at: datetime = Field(default_factory=utcnow)


class EventType(StrEnum):
    office = "office"
    wedding = "wedding"
    date = "date"
    party = "party"
    festival = "festival"
    travel = "travel"
    casual = "casual"
    gym = "gym"
    interview = "interview"


class OutfitCreator(StrEnum):
    agent = "agent"
    user = "user"


class Event(SQLModel, table=True):
    __table_args__ = (Index("ix_event_user_id_starts_at", "user_id", "starts_at"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    title: str = ""
    type: str
    # A wall clock at the event's place, stored as written: a plain DateTime, not the UTC type other timestamps use.
    starts_at: datetime = Field(sa_type=DateTime, index=True)
    ends_at: datetime | None = Field(default=None, sa_type=DateTime)
    place: str
    outdoors: bool = False
    dress_code: str = ""
    audience_tags: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Outfit(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("user_id", "item_key", name="uq_outfit_user_id_item_key"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    item_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    item_key: str  # sorted ids joined by comma: one saved outfit per user and set of items
    name: str
    created_by: str = OutfitCreator.agent
    occasion_tags: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    collage_path: str | None = None
    reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class WearLog(SQLModel, table=True):
    __table_args__ = (Index("ix_wearlog_user_id_worn_on", "user_id", "worn_on"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    outfit_id: int | None = Field(default=None, foreign_key="outfit.id", index=True)
    item_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    worn_on: date = Field(index=True)
    event_id: int | None = Field(default=None, foreign_key="event.id", index=True)
    audience_tags: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    weather_snapshot: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class Feedback(SQLModel, table=True):
    __table_args__ = (
        CheckConstraint("rating IN (-1, 1)", name="ck_feedback_rating"),
        Index("ix_feedback_outfit_id_id", "outfit_id", "id"),
        Index("ix_feedback_user_id", "user_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    outfit_id: int = Field(foreign_key="outfit.id")  # no foreign key to item: ids are never reused
    rating: int
    reason: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ItemSeq(SQLModel, table=True):
    """One row (id 1) holding the highest item id ever issued, so a deleted item's id is never handed out again."""

    id: int = Field(primary_key=True)
    last_id: int


_BUMP: Final = text(
    "UPDATE itemseq SET last_id = max(last_id, (SELECT coalesce(max(id), 0) FROM item)) + 1 WHERE id = 1"
)
_READ: Final = text("SELECT last_id FROM itemseq WHERE id = 1")


@event.listens_for(Item, "before_insert")
def _next_item_id(_mapper: Mapper[Item], conn: Connection, item: Item) -> None:
    """Outfit and WearLog keep item ids as JSON with no foreign key, so an id must never name two garments."""
    if item.id is None:
        conn.execute(_BUMP)  # takes the write lock, so concurrent creators serialise
        item.id = conn.execute(_READ).scalar_one()  # a missing row fails loudly; migrate() always seeds it
