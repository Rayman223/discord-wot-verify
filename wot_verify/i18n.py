import json
from importlib.resources import files

MESSAGES = json.loads(files("wot_verify").joinpath("locales/en.json").read_text())


def t(key):
    """Single message boundary: add locale catalogs and selection here in future."""
    return MESSAGES.get(key, MESSAGES["error"])
