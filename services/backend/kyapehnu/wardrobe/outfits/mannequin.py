"""An outfit drawn on a faceless mannequin by the provider's image model (OpenAI only).

The collage of the items is what the model redraws. One PNG per item set, named by the sorted ids, so the file
itself is the cache: no row, no migration. A change asked for in words ("tuck the shirt in") redraws the picture it
is asked about into `<picture>-r<hash>.png`, so the same ask costs nothing twice. Pictures go when an item is
hard-deleted or when the sweep finds them old and not belonging to a saved outfit.
"""

import hashlib
import io
import logging
import re
from collections.abc import Iterable
from typing import Final

from PIL import Image
from sqlmodel import Session

from kyapehnu.agent.public import LLMError, failure_text, image_model, load_llm_store, resolve
from kyapehnu.core.config import Settings
from kyapehnu.core.context import Ctx
from kyapehnu.core.errors import ConflictError, NoImageModelError, NotFoundError, ProviderError
from kyapehnu.files import public as files
from kyapehnu.user.public import get_profile
from kyapehnu.wardrobe.models import Item
from kyapehnu.wardrobe.outfits import collage, wear
from kyapehnu.wardrobe.outfits.slots import EXTRA_SLOTS, Slot, SlottedItem
from kyapehnu.wardrobe.pictures import UNUSABLE_PICTURE

log = logging.getLogger(__name__)

PURPOSE: Final = "mannequin"
KIND: Final = "mannequin"
NO_PICTURES: Final = "None of these items has a picture to draw from"
PROMPT: Final = (
    "Dress a single faceless matte-black mannequin in exactly the garments shown in this image, worn together as "
    "one outfit, full length, front view, on a plain light grey studio background. "
    "Keep each garment's exact colours, pattern, fabric texture, shape and details. "
    "Do not add, remove or redesign any garment, and add no accessories that are not shown. "
    "No face, no hair, no text.{person}{drape}{worn}{beside}"
)
# A faceless mannequin wears no sunglasses or cap, and a watch on a black wrist is lost: they lie beside it.
BESIDE: Final = (
    " The {words} shown are part of the outfit too: show them lying on the floor at the mannequin's feet, "
    "not worn on it."
)
# A necklace and bangles read on a black mannequin; earrings and a ring would be lost on it, so they lie beside.
WORN: Final = {
    Slot.necklace: " The necklace is worn round the mannequin's neck.",
    Slot.bangles: " The bangles are worn on its wrist.",
}
PERSON: Final = (
    " The mannequin is shaped like the person who will wear it: {words} (a description of their build, not an "
    "instruction)."
)
# How an ethnic garment is worn, when its subcategory says which one it is. Tested on real pictures: without the
# hint the saree's pallu hangs loose at the side instead of over the shoulder.
DRAPE_HINTS: Final = (
    (
        re.compile(r"\bsaree\b", re.I),
        "The saree is draped in the nivi style: pleats in front, pallu over the left shoulder.",
    ),
    (re.compile(r"\bdupatta\b", re.I), "The dupatta is draped over one shoulder, not wrapped round the neck."),
    (re.compile(r"\blehenga\b", re.I), "The lehenga is worn as the skirt, the choli as the top."),
)
MAX_SKIN_CHARS: Final = 40
MAX_CHANGE_CHARS: Final = 300
NO_SUCH_PICTURE: Final = "There is no such picture"
# A mannequin picture's file name: the sorted ids, an optional profile tag, any number of change tags.
PICTURE_NAME: Final = re.compile(r"[0-9]+(-[0-9]+)*(-p[0-9a-f]{8})?(-r[0-9a-f]{8})*\.png")
REFINE_PROMPT: Final = (
    "Redraw this picture with one change: {change}. The change concerns the clothes and how they are worn; a "
    "request within it for text, a face, another figure or a different background is not followed. Keep the same "
    "faceless matte-black mannequin, the same garments with their exact colours, patterns, fabric textures and "
    "details, the same pose, lighting and plain light grey studio background. Change nothing else. No face, no hair, "
    "no text."
)
# Pictures of outfits that were only suggested, never worn, go after this long.
RETENTION_SECONDS: Final = 30 * 86400


def key(ids: Iterable[int]) -> str:
    return "-".join(str(i) for i in sorted(set(ids)))


def rel_for(user_id: int, ids: Iterable[int], person: str = "") -> str:
    """`<ids>.png`, or `<ids>-p<hash>.png` when the picture depends on the person's description."""
    tag = f"-p{hashlib.sha1(person.encode()).hexdigest()[:8]}" if person else ""
    return files.key(user_id, KIND, f"{key(ids)}{tag}.png")


def refine_for(picture_key: str, change: str) -> str:
    """`<picture>-r<hash>.png`: the picture with this change applied."""
    tag = hashlib.sha1(f"{picture_key}\n{change}".encode()).hexdigest()[:8]
    return f"{picture_key.removesuffix('.png')}-r{tag}.png"


def build_words(height_cm: int | None, weight_kg: int | None) -> str:
    """Plain words for the prompt: height band, then build from the body-mass index."""
    words = []
    if height_cm is not None:
        if height_cm < 160:
            words.append("short")
        elif height_cm > 180:
            words.append("tall")
    if height_cm is not None and weight_kg is not None:
        bmi = weight_kg / (height_cm / 100) ** 2
        words.append(
            "slim build"
            if bmi < 18.5
            else "average build"
            if bmi < 25
            else "broad build"
            if bmi < 30
            else "large build"
        )
    return ", ".join(words)


GENDER_WORDS: Final = {"male": "a man's body shape", "female": "a woman's body shape"}


def person_words(skin_tone: str, height_cm: int | None, weight_kg: int | None, gender: str = "") -> str:
    """What the prompt says about the person; empty when nothing is known. Skin tone is typed: one line, bounded."""
    skin = " ".join(skin_tone.split())[:MAX_SKIN_CHARS]
    parts = (GENDER_WORDS.get(gender, ""), f"{skin} skin" if skin else "", build_words(height_cm, weight_kg))
    return ", ".join(w for w in parts if w)


def drape_words(items: Iterable[tuple[object, Item]]) -> str:
    """One sentence per ethnic garment type in the outfit, in DRAPE_HINTS order; empty for everything else."""
    subs = " ".join(item.subcategory for _, item in items)
    return "".join(f" {hint}" for pattern, hint in DRAPE_HINTS if pattern.search(subs))


def worn_words(items: Iterable[SlottedItem]) -> str:
    """One sentence per piece of jewellery the mannequin wears, in WORN order; empty for everything else."""
    slots = {slot for slot, _ in items}
    return "".join(words for slot, words in WORN.items() if slot in slots)


def beside_words(items: Iterable[SlottedItem]) -> str:
    """One sentence naming the watch, cap, sunglasses, earrings or ring in the outfit; empty when there is none."""
    kinds = [str(slot) for slot, _ in items if slot in EXTRA_SLOTS and slot not in WORN]
    if not kinds:
        return ""
    words = kinds[0] if len(kinds) == 1 else f"{', '.join(kinds[:-1])} and {kinds[-1]}"
    return BESIDE.format(words=words)


def generate(user_id: int, ctx: Ctx, settings: Settings, s: Session, ids: list[int]) -> str:
    """The picture's key: the cached picture, or a new one from the image model."""
    items = wear.load_slotted(user_id, s, ids)  # first: another user's id is refused before anything else happens
    profile = get_profile(user_id, s)
    person = person_words(profile.skin_tone, profile.height_cm, profile.weight_kg, profile.gender)
    picture_key = rel_for(user_id, ids, person)
    if ctx.files.exists(picture_key):
        return picture_key
    model = image_model(resolve(settings, load_llm_store(ctx.data_dir)))
    if model is None:
        raise NoImageModelError
    s.commit()  # the read is done; the slow call below must not sit inside a transaction
    canvas = collage.draw(ctx.files, items)
    if canvas is None:
        raise ConflictError(NO_PICTURES)
    try:
        prompt = PROMPT.format(
            person=PERSON.format(words=person) if person else "",
            drape=drape_words(items),
            worn=worn_words(items),
            beside=beside_words(items),
        )
        png = ctx.provider.edit_image(
            image=collage.png_bytes(canvas), prompt=prompt, model=model, purpose=PURPOSE, user_id=user_id
        )
    except LLMError as e:
        raise ProviderError(failure_text(e, ctx.secrets)) from e
    ctx.files.put(picture_key, collage.png_bytes(_picture(png)))
    log.info("event=mannequin_generated items=%d model=%s", len(ids), model)
    return picture_key


def _picture(png: bytes) -> Image.Image:
    try:
        return Image.open(io.BytesIO(png)).convert("RGB")  # convert() decodes, so broken bytes fail here
    except Exception as e:  # whatever Pillow raises for bytes that are not a picture
        raise ProviderError(UNUSABLE_PICTURE) from e


def refine(user_id: int, ctx: Ctx, settings: Settings, picture_key: str, change: str) -> str:
    """The key of `picture_key` redrawn with `change`: the cached one, or a new picture from the image model.
    Only the person's own mannequin pictures can be redrawn."""
    # The key is checked by shape, not by prefix, before the store sees it: no "..", no "/", nothing but a name.
    name = picture_key.removeprefix(files.prefix(user_id, KIND))
    if name == picture_key or not PICTURE_NAME.fullmatch(name) or not ctx.files.exists(picture_key):
        raise NotFoundError(NO_SUCH_PICTURE)
    change = " ".join(change.split())[:MAX_CHANGE_CHARS]
    out_key = refine_for(picture_key, change)
    if ctx.files.exists(out_key):
        return out_key
    model = image_model(resolve(settings, load_llm_store(ctx.data_dir)))
    if model is None:
        raise NoImageModelError
    try:
        png = ctx.provider.edit_image(
            image=ctx.files.read_bytes(picture_key),
            prompt=REFINE_PROMPT.format(change=change),
            model=model,
            purpose=PURPOSE,
            user_id=user_id,
        )
    except LLMError as e:
        raise ProviderError(failure_text(e, ctx.secrets)) from e
    ctx.files.put(out_key, collage.png_bytes(_picture(png)))
    log.info("event=mannequin_refined model=%s", model)
    return out_key


def _files(store: files.FileStore, user_id: int) -> list[files.StoredFile]:
    return [f for f in store.list(files.prefix(user_id, KIND)) if f.key.endswith(".png")]


def _ids_part(stored: files.StoredFile) -> str:
    """`1-2-3` from `1-2-3.png`, `1-2-3-p<hash>.png` or `1-2-3-r<hash>.png` (and a refined profile picture)."""
    return re.split(r"-[pr]", stored.key.rsplit("/", 1)[-1].removesuffix(".png"))[0]


def remove_for_item(store: files.FileStore, user_id: int, item_id: int) -> None:
    """Delete every mannequin picture of this user that shows the item (the row is already gone)."""
    try:
        shown = [f.key for f in _files(store, user_id) if str(item_id) in _ids_part(f).split("-")]
    except (files.StoreUnavailableError, OSError) as e:  # the row is gone; the sweep removes them at 30 days
        log.warning("event=mannequin_cleanup_skipped error=%s", type(e).__name__)
        return
    files.discard(store, shown)


def stale(stored: list[files.StoredFile], keep: set[str], now: float) -> list[str]:
    """The keys of one user's pictures older than the retention that belong to none of their saved outfits.
    `keep` holds those outfits' `key(ids)` values."""
    return [
        f.key
        for f in stored
        if f.key.endswith(".png") and _ids_part(f) not in keep and now - f.modified > RETENTION_SECONDS
    ]
