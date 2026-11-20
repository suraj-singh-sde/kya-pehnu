"""Demo wardrobe: ~30 hand-tagged items with Pillow-drawn silhouettes (no licensed images, no LLM)."""

from typing import Any
from uuid import uuid4

from PIL import Image, ImageDraw
from sqlalchemy import Engine
from sqlmodel import Session, func, select

from kyapehnu.files import public as files
from kyapehnu.wardrobe.ingest import images
from kyapehnu.wardrobe.models import Item


class SeedError(Exception):
    pass


ALL = ["summer", "monsoon", "autumn", "winter", "spring"]
# (name, category, subcategory, hex, color name, pattern, fabric, formality, warmth, seasons)
_ROWS = [
    ("White oxford shirt", "top", "shirt", "#F5F5F0", "white", "solid", "cotton", 4, 2, ["summer", "spring", "autumn"]),
    ("Light blue linen shirt", "top", "shirt", "#A7C7E7", "light blue", "solid", "linen", 3, 1, ["summer", "monsoon"]),
    ("Navy polo", "top", "polo", "#1F2A44", "navy", "solid", "cotton pique", 2, 2, ["summer", "spring", "autumn"]),
    ("Black crew tee", "top", "t-shirt", "#111111", "black", "solid", "cotton", 1, 1, ["summer", "monsoon", "spring"]),
    ("Grey graphic tee", "top", "t-shirt", "#8A8A8A", "grey", "print", "cotton", 1, 1, ["summer", "monsoon"]),
    ("Olive henley", "top", "henley", "#556B2F", "olive", "solid", "cotton", 2, 2, ["autumn", "spring"]),
    ("Maroon striped shirt", "top", "shirt", "#7B1E2B", "maroon", "stripe", "cotton", 3, 2, ["autumn", "winter"]),
    ("Beige knit sweater", "top", "sweater", "#D8C3A5", "beige", "solid", "wool", 3, 4, ["winter"]),
    (
        "Dark indigo jeans",
        "bottom",
        "jeans",
        "#2B3A55",
        "indigo",
        "solid",
        "denim",
        2,
        3,
        ["autumn", "winter", "spring"],
    ),
    (
        "Khaki chinos",
        "bottom",
        "chinos",
        "#C3B091",
        "khaki",
        "solid",
        "cotton twill",
        3,
        2,
        ["summer", "autumn", "spring"],
    ),
    (
        "Charcoal trousers",
        "bottom",
        "trousers",
        "#36454F",
        "charcoal",
        "solid",
        "wool blend",
        4,
        3,
        ["autumn", "winter", "spring"],
    ),
    (
        "Black joggers",
        "bottom",
        "joggers",
        "#1A1A1A",
        "black",
        "solid",
        "cotton fleece",
        1,
        2,
        ["autumn", "winter", "spring"],
    ),
    ("Navy shorts", "bottom", "shorts", "#22335A", "navy", "solid", "cotton", 1, 1, ["summer"]),
    ("Floral midi dress", "dress", "midi dress", "#E8A0BF", "pink", "floral", "rayon", 2, 1, ["summer", "spring"]),
    ("Navy blazer", "outerwear", "blazer", "#1C2841", "navy", "solid", "wool", 5, 3, ["autumn", "winter", "spring"]),
    ("Denim jacket", "outerwear", "jacket", "#5A7BA6", "blue", "solid", "denim", 2, 3, ["autumn", "spring"]),
    ("Black puffer", "outerwear", "puffer", "#0D0D0D", "black", "solid", "polyester", 1, 5, ["winter"]),
    (
        "Tan trench coat",
        "outerwear",
        "trench",
        "#C19A6B",
        "tan",
        "solid",
        "cotton gabardine",
        4,
        3,
        ["autumn", "monsoon"],
    ),
    (
        "White sneakers",
        "footwear",
        "sneakers",
        "#FAFAFA",
        "white",
        "solid",
        "leather",
        1,
        2,
        ["summer", "autumn", "spring"],
    ),
    (
        "Brown oxfords",
        "footwear",
        "oxfords",
        "#5C3A21",
        "brown",
        "solid",
        "leather",
        5,
        2,
        ["autumn", "winter", "spring"],
    ),
    ("Tan suede loafers", "footwear", "loafers", "#A0703A", "tan", "solid", "suede", 3, 2, ["autumn", "spring"]),
    ("Kolhapuri chappals", "footwear", "sandals", "#7A4E2D", "brown", "solid", "leather", 3, 1, ["summer"]),
    ("Waterproof black sneakers", "footwear", "sneakers", "#202020", "black", "solid", "synthetic", 1, 2, ["monsoon"]),
    (
        "Ivory chikankari kurta",
        "ethnic",
        "kurta",
        "#FFFFF0",
        "ivory",
        "embroidered",
        "cotton",
        3,
        1,
        ["summer", "spring"],
    ),
    ("Maroon silk kurta", "ethnic", "kurta", "#800020", "maroon", "solid", "silk", 4, 2, ["autumn", "winter"]),
    ("Navy bandhgala", "ethnic", "bandhgala", "#1B1F3B", "navy", "solid", "wool blend", 5, 3, ["winter"]),
    (
        "Mustard nehru jacket",
        "ethnic",
        "nehru jacket",
        "#E1AD01",
        "mustard",
        "solid",
        "jute silk",
        4,
        2,
        ["autumn", "winter", "spring"],
    ),
    (
        "White churidar",
        "ethnic",
        "churidar",
        "#F8F8F8",
        "white",
        "solid",
        "cotton",
        3,
        1,
        ["summer", "spring", "autumn"],
    ),
    ("Brown leather belt", "accessory", "belt", "#6B4423", "brown", "solid", "leather", 3, 1, ALL),
    ("Silver watch", "accessory", "watch", "#C0C0C0", "silver", "solid", "steel", 4, 1, ALL),
    ("Red check scarf", "accessory", "scarf", "#B22222", "red", "check", "wool", 2, 4, ["winter"]),
    ("Black leather backpack", "bag", "backpack", "#222222", "black", "solid", "leather", 2, 1, ALL),
]
SEED_ITEMS: list[dict[str, Any]] = [
    {
        "name": n,
        "category": c,
        "subcategory": sc,
        "colors": [{"hex": h, "name": cn, "role": "primary"}],
        "pattern": p,
        "fabric": f,
        "formality": fo,
        "warmth": w,
        "seasons": list(se),
        "fit": "regular",
        "tags": ["seed"],
    }
    for n, c, sc, h, cn, p, f, fo, w, se in _ROWS
]

# Normalized (0–1) outlines per category on a square canvas.
_SHAPES = {
    "top": [
        (0.3, 0.15),
        (0.7, 0.15),
        (0.9, 0.35),
        (0.78, 0.45),
        (0.72, 0.4),
        (0.72, 0.9),
        (0.28, 0.9),
        (0.28, 0.4),
        (0.22, 0.45),
        (0.1, 0.35),
    ],
    "bottom": [(0.3, 0.1), (0.7, 0.1), (0.75, 0.9), (0.55, 0.9), (0.5, 0.35), (0.45, 0.9), (0.25, 0.9)],
    "dress": [(0.4, 0.1), (0.6, 0.1), (0.65, 0.35), (0.8, 0.9), (0.2, 0.9), (0.35, 0.35)],
    "outerwear": [
        (0.28, 0.12),
        (0.72, 0.12),
        (0.92, 0.3),
        (0.85, 0.9),
        (0.72, 0.9),
        (0.72, 0.45),
        (0.28, 0.45),
        (0.28, 0.9),
        (0.15, 0.9),
        (0.08, 0.3),
    ],
    "footwear": [(0.1, 0.6), (0.55, 0.45), (0.7, 0.55), (0.9, 0.62), (0.9, 0.75), (0.1, 0.75)],
    "ethnic": [
        (0.35, 0.08),
        (0.65, 0.08),
        (0.85, 0.3),
        (0.75, 0.38),
        (0.7, 0.33),
        (0.75, 0.95),
        (0.25, 0.95),
        (0.3, 0.33),
        (0.25, 0.38),
        (0.15, 0.3),
    ],
    "accessory": [(0.2, 0.45), (0.8, 0.45), (0.8, 0.55), (0.2, 0.55)],
    "bag": [(0.25, 0.3), (0.75, 0.3), (0.8, 0.9), (0.2, 0.9)],
}


def draw_silhouette(category: str, hex_: str, pattern: str, size: int = 512) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    pts = [(x * size, y * size) for x, y in _SHAPES[category]]
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    fill = Image.new("RGBA", (size, size), hex_)
    d = ImageDraw.Draw(fill)
    if pattern in ("stripe", "check"):
        for x in range(0, size, 24):
            d.line([(0, x), (size, x)], fill="#00000055", width=6)
            if pattern == "check":
                d.line([(x, 0), (x, size)], fill="#00000055", width=6)
    elif pattern != "solid":
        for x in range(12, size, 40):
            for y in range(12, size, 40):
                d.ellipse([x, y, x + 10, y + 10], fill="#00000066")
    im.paste(fill, mask=mask)
    ImageDraw.Draw(im).polygon(pts, outline="#555555", width=3)
    return im


def seed(user_id: int, engine: Engine, store: files.FileStore, force: bool = False) -> int:
    run_id = uuid4().hex[:8]  # a --force run must not share files with the first one
    written: list[str] = []
    with Session(engine) as s:
        if not force and s.exec(select(func.count()).select_from(Item).where(Item.user_id == user_id)).one():
            raise SeedError("this wardrobe already has items; use --force to add seed items anyway")
        try:
            for i, raw in enumerate(SEED_ITEMS):
                im = draw_silhouette(raw["category"], raw["colors"][0]["hex"], raw["pattern"])
                cut, thumb = images.save_cutout(im, store, user_id, f"seed_{run_id}_{i}")
                written += [cut, thumb]
                s.add(Item(user_id=user_id, **raw, cutout_path=cut, thumb_path=thumb, phash=images.phash(im)))
            s.commit()
        except Exception:
            files.discard(store, written)  # the rows are rolled back when the session closes
            raise
    return len(SEED_ITEMS)
