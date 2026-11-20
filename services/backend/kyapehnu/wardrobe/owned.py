"""Lookups of items by id that see one user's rows only. Kept apart so `items` and `outfits.wear` can both use them."""

from collections.abc import Collection, Sequence
from typing import cast

from sqlmodel import Session, col, select

from kyapehnu.core.errors import NotFoundError
from kyapehnu.wardrobe.models import Item


def find_items(user_id: int, s: Session, ids: Collection[int]) -> dict[int, Item]:
    """The items of this user among `ids`; a missing one, or one of another user, is simply absent."""
    if not ids:
        return {}
    found = s.exec(select(Item).where(Item.user_id == user_id, col(Item.id).in_(ids)))
    return {cast(int, item.id): item for item in found}  # saved rows have an id


def require_owned_items(user_id: int, s: Session, ids: Sequence[int]) -> dict[int, Item]:
    """Every id must be an item of this user, else NotFoundError (AC-20). Call it before ids are stored in JSON."""
    found = find_items(user_id, s, ids)
    for item_id in ids:
        if item_id not in found:
            raise NotFoundError(f"Item {item_id} not found")
    return found
