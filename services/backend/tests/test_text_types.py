"""Cleaned-text types shared by every Phase 2 request model (LLD 3.2): control characters stripped, trimmed,
and limits checked on the cleaned value."""

from typing import Annotated

import pytest
from pydantic import Field, TypeAdapter, ValidationError

from kyapehnu.core.schemas import ItemIds, Line, Para, clean_line, clean_para


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("  hello  ", "hello"),
        ("a\x00b", "ab"),
        ("a\x07b\x1fc", "abc"),
        ("zero​width", "zerowidth"),  # zero-width space (Cf)
        ("‮evil", "evil"),  # right-to-left override (Cf)
    ],
)
def test_clean_line_strips_control_characters_and_trims(raw, cleaned):
    assert clean_line(raw) == cleaned


@pytest.mark.parametrize("raw", ["one\ntwo", "one\r\ntwo", "\n\none\n"])
def test_clean_line_never_returns_a_line_break(raw):
    result = clean_line(raw)

    assert isinstance(result, str)
    assert "\n" not in result
    assert "\r" not in result
    assert "one" in result


@pytest.mark.parametrize("value", [42, None, ["a"]])
def test_clean_line_leaves_non_strings_for_the_validator_to_reject(value):
    assert clean_line(value) == value


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("a\nb", "a\nb"),
        ("a\x00\nb", "a\nb"),
        ("​  x  \n", "x"),
    ],
)
def test_clean_para_keeps_line_breaks_strips_controls_and_trims(raw, cleaned):
    assert clean_para(raw) == cleaned


@pytest.mark.parametrize("value", [42, None])
def test_clean_para_leaves_non_strings_for_the_validator_to_reject(value):
    assert clean_para(value) == value


def test_line_checks_the_limit_on_the_cleaned_value():
    adapter = TypeAdapter(Annotated[Line, Field(max_length=5)])

    assert adapter.validate_python("ab" + "\x00" * 50) == "ab"
    with pytest.raises(ValidationError):
        adapter.validate_python("abcdef")


def test_line_that_is_empty_after_cleaning_fails_a_min_length():
    adapter = TypeAdapter(Annotated[Line, Field(min_length=1, max_length=40)])

    with pytest.raises(ValidationError):
        adapter.validate_python("\x00​  ")


def test_line_rejects_a_non_string():
    with pytest.raises(ValidationError):
        TypeAdapter(Annotated[Line, Field(max_length=5)]).validate_python(5)


def test_para_keeps_newlines_and_checks_the_limit_on_the_cleaned_value():
    adapter = TypeAdapter(Annotated[Para, Field(max_length=5)])

    assert adapter.validate_python("a\nb" + "\x00" * 50) == "a\nb"
    with pytest.raises(ValidationError):
        adapter.validate_python("ab\ncdef")


@pytest.mark.parametrize("ids", [[1], [3, 1, 2], list(range(1, 11))])
def test_item_ids_accepts_one_to_ten_distinct_positive_ids(ids):
    assert TypeAdapter(ItemIds).validate_python(ids) == ids


@pytest.mark.parametrize("ids", [[], list(range(1, 12)), [0], [-3], [1, 2, 2]])
def test_item_ids_rejects_empty_too_many_non_positive_and_repeated_ids(ids):
    with pytest.raises(ValidationError):
        TypeAdapter(ItemIds).validate_python(ids)


def test_item_ids_repeat_is_reported_as_distinct_ids_message():
    with pytest.raises(ValidationError) as exc:
        TypeAdapter(ItemIds).validate_python([4, 4])

    assert "item ids must be distinct" in str(exc.value)
