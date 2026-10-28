import json
from importlib.resources import files

SUPPORTED_LOCALES = ("en", "fr")
CATALOGS = {
    locale: json.loads(
        files("wot_verify").joinpath(f"locales/{locale}.json").read_text(encoding="utf-8")
    )
    for locale in SUPPORTED_LOCALES
}
MESSAGES = CATALOGS["en"]


def t(key, locale="en"):
    """Translate a message, falling back to English for missing keys or locales."""
    return CATALOGS.get(locale, MESSAGES).get(key, MESSAGES.get(key, MESSAGES["error"]))
