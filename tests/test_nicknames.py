import pytest

from wot_verify.nicknames import without_birthday_candles, without_managed_tag


@pytest.mark.parametrize(
    "name,expected",
    [
        (None, None),
        ("Player", "Player"),
        (" Player ", " Player "),
        ("Player  [TAG]", "Player  [TAG]"),
        ("🎂Player", "🎂Player"),
        ("🕯Player", "Player"),
        ("Player🕯🕯", "Player"),
        ("Player 🕯️🕯️", "Player"),
        ("🕯︎ Player 🕯️", "Player"),
        ("Player [TAG] 🕯", "Player [TAG]"),
        ("🕯️ Player 🕯 [TAG] 🕯️", "Player [TAG]"),
    ],
)
def test_comparison_ignores_only_candles_and_their_variation_selectors(name, expected):
    assert without_birthday_candles(name) == expected


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Player [TAG]", "Player"),
        ("Player [TAG] 🕯️🕯", "Player 🕯️🕯"),
        ("🕯 Player [TAG]", "🕯 Player"),
        ("Player 🕯 [TAG] 🕯️", "Player 🕯 🕯️"),
        ("Player [T🕯AG]", "Player🕯"),
        ("Custom [ALT] [TAG] 🕯", "Custom [ALT] 🕯"),
        ("Player [OTHER] 🕯", None),
        ("Player [TAG] custom 🕯", None),
        ("Player[TAG] 🕯", None),
    ],
)
def test_managed_tag_cleanup_preserves_candles_and_unrelated_tags(name, expected):
    assert without_managed_tag(name, " [TAG]") == expected
