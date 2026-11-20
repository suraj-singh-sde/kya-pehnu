import logging

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlmodel import Session

from kyapehnu.agent.llm.base import LLMProvider, LLMUnavailableError
from kyapehnu.core.config import DedupePrefs
from kyapehnu.core.db import make_engine, migrate
from kyapehnu.core.schemas import TAG_FIELDS, GarmentTags, ItemPatch, TagsPatch
from kyapehnu.files.errors import StoreUnavailableError
from kyapehnu.files.local import LocalStore
from kyapehnu.wardrobe.ingest.dedupe import find_duplicate
from kyapehnu.wardrobe.ingest.tagging import PhotoAnalysis, analyze_photo
from kyapehnu.wardrobe.models import Item
from tests.fakes import OWNER_ID, FakeTransport, analysis, ensure_owner, garment


def test_analyze_photo_sends_one_image_and_parses():
    t = FakeTransport([analysis(garment(), garment(category="bottom"), photo_type="worn")])
    out = analyze_photo(OWNER_ID, LLMProvider(t), b"\xff\xd8jpeg")
    assert out.photo_type == "worn" and len(out.garments) == 2
    assert t.calls[0]["name"] == "PhotoAnalysis" and len(t.calls[0]["images"]) == 1


def test_color_roles_follow_position():
    g = GarmentTags.model_validate(
        garment(colors=[{"hex": "#000000", "name": "black", "role": "secondary"}, {"hex": "#ffffff", "name": "white"}])[
            "tags"
        ]
    )
    assert [c.role for c in g.colors] == ["primary", "secondary"]


@pytest.mark.parametrize(
    "override",
    [{"formality": 7}, {"colors": [{"hex": "red", "name": "red"}]}, {"category": "hat"}, {"colors": []}],
)
def test_garment_tags_reject_bad_values(override):
    with pytest.raises(ValidationError):
        GarmentTags.model_validate(garment(**override)["tags"])


@pytest.mark.parametrize("bbox", [[0.1, 0.1, 0.5], [0.1, 0.1, 0.5, 0.5, 0.9], [float("nan"), 0, 1, 1]])
def test_analysis_rejects_bad_bbox(bbox):
    with pytest.raises(ValidationError):
        PhotoAnalysis.model_validate(analysis(garment(bbox=bbox)))


def test_patch_schemas():
    assert set(TAG_FIELDS) == set(TagsPatch.model_fields) == set(GarmentTags.model_fields)
    assert set(ItemPatch.model_fields) - set(TAG_FIELDS) == {"brand", "purchase_price", "purchase_date", "notes"}
    assert ItemPatch().model_dump(exclude_unset=True) == {}
    with pytest.raises(ValidationError):
        ItemPatch(purchase_price=-1)
    with pytest.raises(ValidationError):
        TagsPatch(warmth=0)
    patch = TagsPatch.model_validate({"colors": [{"hex": "#000000", "name": "b", "role": "secondary"}]})
    assert patch.colors[0].role == "primary"


@pytest.fixture
def db(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)
    LocalStore(tmp_path)  # creates images/
    with Session(engine) as s:
        yield s, tmp_path


def _item(s, data_dir, name, hex_, phash, category="top", pattern="solid", status="available"):
    stem = name.replace(" ", "_")
    (data_dir / "images/1/thumb").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 40), hex_).save(data_dir / f"images/1/thumb/{stem}.webp")
    it = Item(
        user_id=OWNER_ID,
        name=name,
        category=category,
        pattern=pattern,
        status=status,
        phash=phash,
        thumb_path=f"images/1/thumb/{stem}.webp",
        colors=[{"hex": hex_, "name": "x", "role": "primary"}],
    )
    ensure_owner(s)
    s.add(it)
    s.commit()
    return it


NAVY = GarmentTags.model_validate(garment(colors=[{"hex": "#1F2A44", "name": "navy"}])["tags"])
PH_A, PH_FAR = "0000000000000000", "ffffffffffffffff"


def _find(s, d, transport, tags=NAVY):
    return find_duplicate(
        OWNER_ID,
        s,
        cutout_phash=PH_A,
        tags=tags,
        cutout_png=b"new",
        provider=LLMProvider(transport),
        prefs=DedupePrefs(),
        store=LocalStore(d),
    )


def test_dedupe_near_identical_phash_needs_no_llm(db):
    s, d = db
    it = _item(s, d, "navy polo", "#1F2A44", "0000000000000003")
    t = FakeTransport()
    assert _find(s, d, t) == (it.id, "near-identical image")
    assert t.calls == []


def test_dedupe_phash_picks_closest(db):
    s, d = db
    _item(s, d, "far polo", "#1F2A44", "000000000000000f")
    near = _item(s, d, "near polo", "#1F2A44", "0000000000000001")
    assert _find(s, d, FakeTransport())[0] == near.id


def test_dedupe_similar_color_asks_llm_and_trusts_confident_match(db):
    s, d = db
    _item(s, d, "black tee", "#000000", PH_FAR)
    navy = _item(s, d, "navy polo", "#22335A", PH_FAR)
    t = FakeTransport([{"match_index": 1, "confidence": 0.9}])
    item_id, reason = _find(s, d, t)
    assert item_id == navy.id and "navy polo" in reason
    assert len(t.calls[0]["images"]) == 2  # new cutout + 1 candidate (black is too far in delta E)


def test_dedupe_low_confidence_is_ignored(db):
    s, d = db
    _item(s, d, "navy polo", "#22335A", PH_FAR)
    assert _find(s, d, FakeTransport([{"match_index": 1, "confidence": 0.5}])) == (None, None)


@pytest.mark.parametrize("index", [None, 0, 2])
def test_dedupe_out_of_range_or_null_index_is_ignored(db, index):
    s, d = db
    _item(s, d, "navy polo", "#22335A", PH_FAR)
    assert _find(s, d, FakeTransport([{"match_index": index, "confidence": 0.99}])) == (None, None)


def test_dedupe_other_category_or_retired_not_candidates(db):
    s, d = db
    _item(s, d, "navy jeans", "#1F2A44", PH_FAR, category="bottom")
    _item(s, d, "old navy polo", "#1F2A44", "0000000000000001", status="retired")
    t = FakeTransport()
    assert _find(s, d, t) == (None, None)
    assert t.calls == []


def test_dedupe_llm_failure_is_silent_and_logged(db, caplog):
    s, d = db
    _item(s, d, "navy polo", "#22335A", PH_FAR)
    with caplog.at_level(logging.INFO):
        assert _find(s, d, FakeTransport([LLMUnavailableError("down: sk-leaky")])) == (None, None)
    assert "event=dedupe_skipped error=LLMUnavailableError" in caplog.text
    assert "sk-leaky" not in caplog.text


def test_dedupe_missing_thumb_file_skips_check(db, caplog):
    s, d = db
    it = _item(s, d, "navy polo", "#22335A", PH_FAR)
    (d / it.thumb_path).unlink()
    t = FakeTransport()
    with caplog.at_level(logging.INFO):
        assert _find(s, d, t) == (None, None)
    assert t.calls == [] and "event=dedupe_skipped error=MissingFileError" in caplog.text


def test_dedupe_store_outage_skips_check(db, caplog, monkeypatch):
    s, d = db
    _item(s, d, "navy polo", "#22335A", PH_FAR)

    def down(self, key):
        raise StoreUnavailableError

    monkeypatch.setattr(LocalStore, "read_bytes", down)
    t = FakeTransport()
    with caplog.at_level(logging.INFO):
        assert _find(s, d, t) == (None, None)
    assert t.calls == [] and "event=dedupe_skipped error=StoreUnavailableError" in caplog.text


def test_tagging_prompt_names_every_field_and_every_allowed_value():
    """Models that are not forced to follow the schema (OpenAI-compatible servers) go by the prompt alone:
    gpt-4o left `pattern` out of every first reply until the prompt mentioned it."""
    from kyapehnu.wardrobe.ingest import tagging
    from kyapehnu.wardrobe.models import Category, Pattern, PhotoType, Season

    for field in tagging.DetectedGarment.model_fields:
        assert f"{field}:" in tagging.SYSTEM, field
    for enum in (Category, Pattern, Season, PhotoType):
        for value in enum:
            assert value.value in tagging.SYSTEM, value


FLAT = {
    "bbox": [0.1, 0.1, 0.9, 0.9],
    "main": True,
    "name": "Striped shirt",
    "category": "top",
    "subcategory": "shirt",
    "colors": [{"hex": "#112233", "name": "navy"}],
    "surface_pattern": "stripe",
    "fabric": None,
    "formality": 3,
    "warmth": 2,
    "seasons": ["summer"],
    "fit": None,
    "keywords": ["office"],
}


def test_the_model_is_asked_for_a_flat_garment_with_unambiguous_field_names():
    """gpt-4o always dropped a property called `pattern`, and mixed up a `tags` object holding a `tags` list."""
    from kyapehnu.wardrobe.ingest import tagging

    schema = tagging.PhotoAnalysis.model_json_schema()
    garment_schema = schema["$defs"]["DetectedGarment"]

    assert {"surface_pattern", "keywords", "bbox", "main", "name"} <= set(garment_schema["properties"])
    assert not {"pattern", "tags"} & set(garment_schema["properties"])
    assert "surface_pattern" in garment_schema["required"]


def test_a_flat_reply_becomes_the_usual_tags():
    from kyapehnu.wardrobe.ingest import tagging

    g = tagging.DetectedGarment.model_validate(FLAT)

    assert (g.tags.pattern, g.tags.tags, g.tags.name, g.main) == ("stripe", ["office"], "Striped shirt", True)
    assert g.tags.colors[0].role == "primary"


@pytest.mark.parametrize(
    ("subcategory", "name", "category"),
    [
        ("pyjama", "White pyjama", "bottom"),
        ("ethnic bottom", "Cream pajamas", "bottom"),
        ("churidar", "White churidar", "bottom"),
        ("kurta", "Cream kurta", "ethnic"),
        ("kurta pyjama", "Kurta pyjama set", "ethnic"),
        ("earrings", "Gold jhumkas", "accessory"),
        ("", "Kundan necklace", "accessory"),
        ("blouse", "Saree blouse", "ethnic"),
        ("kurti", "Ring-neck kurti", "ethnic"),
        ("dupatta", "Chain-print dupatta", "ethnic"),
    ],
)
def test_the_trousers_of_a_traditional_set_go_in_the_bottoms_and_jewellery_in_accessories(subcategory, name, category):
    from kyapehnu.wardrobe.ingest import tagging

    g = tagging.DetectedGarment.model_validate(FLAT | {"category": "ethnic", "subcategory": subcategory, "name": name})

    assert (g.category, g.tags.category) == (category, category)


def test_the_prompt_names_womens_kinds():
    from kyapehnu.wardrobe.ingest import tagging

    for words in (
        "kurti",
        "saree and its blouse",
        "lehenga and its choli",
        "leggings",
        "heels",
        "earrings",
        "necklace",
        "bangles",
    ):
        assert words in tagging.SYSTEM
    # A ring on its own is a photo of jewellery, not a photo with no clothing in it (seen live: an empty list).
    assert "no clothing, footwear, bag, accessory or jewellery in the photo" in tagging.SYSTEM
    assert "maang tikka is one pendant" in tagging.SYSTEM


def test_the_older_nested_reply_shape_is_still_understood():
    from kyapehnu.wardrobe.ingest import tagging

    g = tagging.DetectedGarment.model_validate(garment(pattern="check", tags=["a"]))

    assert (g.tags.pattern, g.tags.tags, g.main) == ("check", ["a"], False)


@pytest.mark.parametrize("change", [{"colors": [{"hex": "red", "name": "red"}]}, {"formality": 9}, {"colors": []}])
def test_a_bad_flat_reply_is_rejected_while_parsing_so_the_retry_can_fix_it(change):
    from kyapehnu.wardrobe.ingest import tagging

    with pytest.raises(ValidationError):
        tagging.PhotoAnalysis.model_validate({"photo_type": "worn", "garments": [FLAT | change]})
