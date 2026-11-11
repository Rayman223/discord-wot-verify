"""Nickname decorations tolerated independently from the verified WG identity."""

import re

_CANDLES = re.compile("🕯[\ufe0e\ufe0f]?(?:x[1-9][0-9]*(?![0-9A-Za-z_]))?")


def without_birthday_candles(nickname):
    """Ignore candle decorations without modifying the actual Discord nickname."""
    if nickname is None:
        return None
    stripped = _CANDLES.sub("", nickname)
    # Only collapse spaces when removing candles: other names stay strict.
    return " ".join(stripped.split()) if stripped != nickname else nickname


def without_managed_tag(nickname, suffix):
    """Remove a confirmed trailing clan tag while retaining birthday candles."""
    if not without_birthday_candles(nickname).endswith(suffix):
        return None
    candles = f"(?:{_CANDLES.pattern})*"
    tag = candles.join(re.escape(char) for char in suffix.lstrip())
    match = re.search(tag + f"(?=(?:\\s|{_CANDLES.pattern})*$)", nickname)
    if match is None:
        return None
    decorations = "".join(_CANDLES.findall(match[0]))
    return nickname[: match.start()].rstrip() + decorations + nickname[match.end() :]
