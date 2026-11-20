from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from kyapehnu.auth.public import CurrentUser
from kyapehnu.core.deps import CtxDep, SessionDep, SettingsDep
from kyapehnu.wardrobe.outfits import mannequin, service, wear
from kyapehnu.wardrobe.outfits.schemas import (
    MannequinIn,
    MannequinOut,
    OutfitOut,
    RefineIn,
    SuggestIn,
    SuggestionOut,
    SuggestOut,
    SwapIn,
    WearIn,
    WearLogOut,
    WearsQuery,
)

# The router has no prefix: each route carries its full path.
router = APIRouter(tags=["outfits"])


@router.post("/api/outfits/suggest")
def suggest_outfits(body: SuggestIn, s: SessionDep, ctx: CtxDep, user: CurrentUser) -> SuggestOut:
    return service.suggest(user.id, ctx, s, body, date.today())


@router.post("/api/outfits/swap")
def swap_item(body: SwapIn, s: SessionDep, ctx: CtxDep, user: CurrentUser) -> SuggestionOut:
    return service.swap(user.id, ctx, s, body, date.today())


@router.post("/api/outfits/wear", status_code=201)
def wear_outfit(body: WearIn, s: SessionDep, ctx: CtxDep, user: CurrentUser) -> OutfitOut:
    return wear.wear(user.id, ctx, s, body, date.today())


@router.post("/api/outfits/mannequin")
def mannequin_picture(
    body: MannequinIn, s: SessionDep, ctx: CtxDep, settings: SettingsDep, user: CurrentUser
) -> MannequinOut:
    return MannequinOut(path=mannequin.generate(user.id, ctx, settings, s, body.item_ids))


@router.post("/api/outfits/mannequin/refine")
def refine_mannequin(body: RefineIn, ctx: CtxDep, settings: SettingsDep, user: CurrentUser) -> MannequinOut:
    return MannequinOut(path=mannequin.refine(user.id, ctx, settings, body.path, body.instruction))


@router.get("/api/outfits")
def list_outfits(s: SessionDep, user: CurrentUser) -> list[OutfitOut]:
    return wear.list_outfits(user.id, s)


@router.get("/api/wears")
def list_wears(q: Annotated[WearsQuery, Query()], s: SessionDep, user: CurrentUser) -> list[WearLogOut]:
    return wear.wear_history(user.id, s, q, date.today())
