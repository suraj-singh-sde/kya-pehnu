"""`safe_next` (LLD 3.2; AC-9): only a same-origin path survives, everything else becomes the home page."""

import pytest

from kyapehnu.auth.oauth import safe_next

MAX_LEN = 2048


@pytest.mark.parametrize(
    "raw",
    ["/", "/wardrobe", "/ok?a=1", "/wardrobe/items/3?tab=notes#top", "/" + "a" * (MAX_LEN - 1)],
)
def test_a_local_path_is_kept(raw):
    assert safe_next(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "//evil.example",
        "///evil.example",
        "/\\evil.example",
        "/a\\b",
        "https://evil.example/x",
        "http://localhost/x",
        "javascript:alert(1)",
        "evil.example",
        "wardrobe",
        "\t/",
        " /ok",
        "/ok\n",
        "/ok\r\nSet-Cookie: x=1",
        "/ok\x00",
        "/ok\x1f",
        "/ok\x7f",
        "/" + "a" * MAX_LEN,
        "/" + "a" * 3000,
    ],
)
def test_anything_else_becomes_the_home_page(raw):
    assert safe_next(raw) == "/"
