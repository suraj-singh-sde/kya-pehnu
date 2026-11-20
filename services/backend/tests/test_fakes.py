"""The shared test helpers add_item and add_wear (LLD 7). Later packages build their data with them."""

from datetime import date

from sqlmodel import Session, select

from kyapehnu.wardrobe.models import Item, Outfit, WearLog
from tests.fakes import add_item, add_wear


def test_add_item_commits_a_default_item_that_another_session_can_read(engine):
    with Session(engine) as s:
        item = add_item(s)
        item_id = item.id

    with Session(engine) as other:
        assert item_id is not None
        assert other.get(Item, item_id) is not None


def test_add_item_applies_the_given_fields(engine):
    with Session(engine) as s:
        item = add_item(s, name="Kurta", category="ethnic", formality=4, warmth=1, tags=["festive"])

    assert (item.name, item.category, item.formality, item.warmth, item.tags) == ("Kurta", "ethnic", 4, 1, ["festive"])


def test_add_item_keeps_explicit_empty_and_none_values(engine):
    with Session(engine) as s:
        item = add_item(s, subcategory="", tags=[], notes="", fabric=None)

    assert (item.subcategory, item.tags, item.notes, item.fabric) == ("", [], "", None)


def test_add_wear_logs_an_outfit_wear_with_its_date_audience_and_items(engine):
    with Session(engine) as s:
        a_id, b_id = add_item(s).id, add_item(s).id
        wear_id = add_wear(s, [b_id, a_id], date(2026, 10, 1), audience=["Priya"]).id

    with Session(engine) as other:
        row = other.get(WearLog, wear_id)
        assert (row.worn_on, row.audience_tags, row.item_ids) == (date(2026, 10, 1), ["Priya"], [b_id, a_id])
        assert row.outfit_id is not None
        assert other.get(Outfit, row.outfit_id).item_key == "1,2"


def test_add_wear_defaults_to_no_audience(engine):
    with Session(engine) as s:
        wear = add_wear(s, [add_item(s).id], date(2026, 10, 1))

        assert wear.audience_tags == []


def test_add_wear_reuses_the_outfit_of_the_same_items_in_any_order(engine):
    with Session(engine) as s:
        a_id, b_id = add_item(s).id, add_item(s).id
        first_outfit = add_wear(s, [a_id, b_id], date(2026, 10, 1)).outfit_id
        second_outfit = add_wear(s, [b_id, a_id], date(2026, 10, 2)).outfit_id

        assert first_outfit == second_outfit
        assert len(s.exec(select(Outfit)).all()) == 1


def test_add_wear_without_outfit_logs_an_item_only_wear(engine):
    with Session(engine) as s:
        wear = add_wear(s, [add_item(s).id], date(2026, 10, 1), outfit=False)

        assert wear.outfit_id is None
        assert s.exec(select(Outfit)).all() == []
        assert len(s.exec(select(WearLog)).all()) == 1
