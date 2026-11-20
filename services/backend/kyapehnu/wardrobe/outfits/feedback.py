"""Thumbs up and down on outfits. The only writer of the feedback table."""

import logging
from typing import cast

from sqlmodel import Session

from kyapehnu.core.context import Ctx
from kyapehnu.core.db import get_owned
from kyapehnu.wardrobe.models import Feedback, Outfit
from kyapehnu.wardrobe.outfits import wear
from kyapehnu.wardrobe.outfits.schemas import FeedbackIn, FeedbackOut

log = logging.getLogger(__name__)


def record(user_id: int, ctx: Ctx, s: Session, body: FeedbackIn) -> FeedbackOut:
    """Store a thumb for a saved outfit, or for a set of items that is saved first (never worn, AC-2).

    The collage is drawn before any write. When the commit fails, the drawn file goes with it.
    """
    drawn = None
    if body.outfit_id is not None:
        outfit = get_owned(user_id, s, Outfit, body.outfit_id)
    else:
        # The schema guarantees one target.
        outfit, drawn = wear.save_outfit(user_id, ctx, s, cast(list[int], body.item_ids))
    row = Feedback(user_id=user_id, outfit_id=cast(int, outfit.id), rating=body.rating, reason=body.reason)
    try:
        s.add(row)
        s.flush()
        saved = FeedbackOut.model_validate(row, from_attributes=True)  # before the commit: nothing is read after it
        s.commit()
    except Exception:
        s.rollback()
        wear.discard_collage(ctx.files, drawn)
        raise
    log.info("event=feedback_recorded outfit_id=%s rating=%d", saved.outfit_id, saved.rating)
    return saved
