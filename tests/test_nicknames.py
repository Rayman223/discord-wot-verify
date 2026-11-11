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
        ("Player [TAG] 🕯x14", "Player [TAG]"),
        ("Player 🕯️x8", "Player"),
        ("Player 🕯︎x25", "Player"),
        ("Playerx14", "Playerx14"),
        ("Player 🕯x14name", "Player x14name"),
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
        ("Player [TAG] 🕯x14", "Player 🕯x14"),
        ("🕯x8 Player [TAG]", "🕯x8 Player"),
        ("Player 🕯x25 [TAG] 🕯️x14", "Player 🕯x25 🕯️x14"),
    ],
)
def test_managed_tag_cleanup_preserves_candles_and_unrelated_tags(name, expected):
    assert without_managed_tag(name, " [TAG]") == expected
