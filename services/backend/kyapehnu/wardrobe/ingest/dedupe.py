import logging
from typing import Final

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from kyapehnu.agent.public import LLMError, LLMProvider
from kyapehnu.core.config import DedupePrefs
from kyapehnu.core.schemas import GarmentTags
from kyapehnu.files import public as files
from kyapehnu.wardrobe.color import delta_e
from kyapehnu.wardrobe.ingest.images import hamming
from kyapehnu.wardrobe.models import Item, ItemStatus

log = logging.getLogger(__name__)
SYSTEM = "You compare clothing photos to find duplicates in a wardrobe catalogue."
MAX_LLM_CANDIDATES: Final = 3


class DedupeVerdict(BaseModel):
    match_index: int | None = None
    confidence: float = Field(ge=0, le=1)


def _skip(reason: Exception) -> tuple[None, None]:
    log.info("event=dedupe_skipped error=%s", type(reason).__name__)  # class only: messages can echo replies
    return None, None


def find_duplicate(
    user_id: int,
    s: Session,
    *,
    cutout_phash: str,
    tags: GarmentTags,
    cutout_png: bytes,
    provider: LLMProvider,
    prefs: DedupePrefs,
    store: files.FileStore,
) -> tuple[int | None, str | None]:
    """Return (item_id, reason) for a likely existing duplicate, else (None, None).

    A down LLM or store is not an error: the check is skipped.
    """
    items = s.exec(select(Item).where(Item.user_id == user_id, Item.status != ItemStatus.retired)).all()
    near = [(hamming(cutout_phash, it.phash), it) for it in items if it.phash]
    near = [(d, it) for d, it in near if d <= prefs.item_phash_distance]
    if near:
        return min(near, key=lambda x: x[0])[1].id, "near-identical image"

    primary = tags.colors[0].hex
    scored = [
        (delta_e(primary, it.colors[0]["hex"]), it)
        for it in items
        if it.category == tags.category and it.pattern == tags.pattern and it.colors and it.thumb_path
    ]
    candidates = [it for d, it in sorted(scored, key=lambda x: x[0]) if d < prefs.max_delta_e][:MAX_LLM_CANDIDATES]
    if not candidates:
        return None, None

    k = len(candidates)
    prompt = (
        f"Image 1 is a newly photographed garment. Images 2 to {k + 1} are candidates already in the wardrobe "
        f"(candidate 1 is image 2, and so on). Is the new garment the same physical item as one of the "
        f"candidates, not merely similar? Set match_index to that candidate number (1 to {k}) or null, "
        f"and confidence from 0 to 1."
    )
    try:
        thumbs = [store.read_bytes(str(it.thumb_path)) for it in candidates]
        verdict = provider.complete_json(
            system=SYSTEM,
            prompt=prompt,
            schema=DedupeVerdict,
            purpose="dedupe",
            images=[cutout_png, *thumbs],
            user_id=user_id,
        )
    except (LLMError, OSError, files.StoreUnavailableError, files.MissingFileError) as e:
        # a missing thumb, a down store or a down LLM must not fail the photo
        return _skip(e)
    if verdict.match_index and 1 <= verdict.match_index <= k and verdict.confidence >= prefs.llm_confidence:
        match = candidates[verdict.match_index - 1]
        return match.id, f"looks like '{match.name}'"
    return None, None
