from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel
from sqlmodel import SQLModel

from kyapehnu.auth.public import CurrentUser
from kyapehnu.core.db import get_owned
from kyapehnu.core.deps import CtxDep, PathId, SessionDep, SettingsDep
from kyapehnu.core.schemas import BulkItemIds, ItemPatch
from kyapehnu.wardrobe import items, pictures
from kyapehnu.wardrobe.items import WardrobeQuery
from kyapehnu.wardrobe.models import Item, ItemCorrection, ItemStatus
from kyapehnu.wardrobe.outfits import wear
from kyapehnu.wardrobe.outfits.schemas import OutfitOut, WearLogOut

router = APIRouter(prefix="/api/items", tags=["items"])


class ItemDetail(SQLModel):
    item: Item
    siblings: list[Item]
    corrections: list[ItemCorrection]
    photo_path: str | None
    wears: list[WearLogOut]
    outfits: list[OutfitOut]


class StatusIn(BaseModel):
    status: ItemStatus


class BulkStatusIn(StatusIn):
    item_ids: BulkItemIds


@router.get("")
def list_items(s: SessionDep, user: CurrentUser, query: Annotated[WardrobeQuery, Query()]) -> list[Item]:
    return items.list_items(user.id, s, **query.model_dump(), today=date.today())


@router.post("/status")
def set_status_many(body: BulkStatusIn, s: SessionDep, user: CurrentUser) -> list[Item]:
    return items.set_status_many(user.id, s, body.item_ids, body.status)


@router.get("/{item_id}")
def get_item(item_id: PathId, s: SessionDep, user: CurrentUser) -> ItemDetail:
    item, siblings, corrections, photo_path = items.get_detail(user.id, s, item_id)
    wears, outfits = wear.item_history(user.id, s, item_id)
    return ItemDetail(
        item=item, siblings=siblings, corrections=corrections, photo_path=photo_path, wears=wears, outfits=outfits
    )


@router.patch("/{item_id}")
def patch_item(item_id: PathId, body: ItemPatch, s: SessionDep, user: CurrentUser) -> Item:
    return items.patch_item(user.id, s, item_id, body.model_dump(exclude_unset=True))


@router.post("/{item_id}/status")
def set_status(item_id: PathId, body: StatusIn, s: SessionDep, user: CurrentUser) -> Item:
    return items.set_status(user.id, s, item_id, body.status)


@router.post(
    "/{item_id}/worn-today",
    status_code=201,
    responses={200: {"model": WearLogOut, "description": "Already logged today"}},
)
def worn_today(item_id: PathId, s: SessionDep, user: CurrentUser, response: Response) -> WearLogOut:
    out, created = wear.worn_today(user.id, s, item_id, date.today())
    if not created:
        response.status_code = 200  # already logged today: nothing new was written
    return out


@router.delete("/{item_id}", status_code=204)
def delete_item(item_id: PathId, s: SessionDep, ctx: CtxDep, user: CurrentUser, hard: bool = False) -> Response:
    if hard:
        items.hard_delete(s, get_owned(user.id, s, Item, item_id), ctx.files)
    else:
        items.set_status(user.id, s, item_id, ItemStatus.retired)
    return Response(status_code=204)


@router.post("/{item_id}/clean-picture")
def make_clean_picture(item_id: PathId, s: SessionDep, ctx: CtxDep, settings: SettingsDep, user: CurrentUser) -> Item:
    item = get_owned(user.id, s, Item, item_id)
    pictures.generate(ctx, settings, s, item)
    s.refresh(item)
    return item


@router.post("/{item_id}/clean-picture/switch")
def switch_picture(item_id: PathId, s: SessionDep, user: CurrentUser) -> Item:
    item = get_owned(user.id, s, Item, item_id)
    pictures.switch(s, item)
    s.refresh(item)
    return item


@router.delete("/{item_id}/clean-picture")
def delete_clean_picture(item_id: PathId, s: SessionDep, ctx: CtxDep, user: CurrentUser) -> Item:
    item = get_owned(user.id, s, Item, item_id)
    pictures.remove(ctx, s, item)
    s.refresh(item)
    return item
