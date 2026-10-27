"""The German catalog must stay complete and undamaged.

Hand-editing .po files is how translations silently rot: an entry loses its
text, or a careless replace doubles a sentence. These tests fail loudly
instead.
"""

import re
from pathlib import Path

from app.translations import SUPPORTED_LANGUAGES, gettext_for, normalise

CATALOG = Path(__file__).resolve().parents[2] / "app" / "i18n" / "de" / "LC_MESSAGES" / "messages.po"


def parse_catalog() -> dict[str, str]:
    """Minimal .po reader: handles single-line and continuation entries."""
    entries: dict[str, str] = {}
    msgid: str | None = None
    buffer = ""
    mode = None
    for raw in CATALOG.read_text(encoding="utf-8").split("\n"):
        line = raw.strip()
        if line.startswith("msgid "):
            if mode == "msgstr" and msgid is not None:
                entries[msgid] = buffer
            msgid = re.sub(r'^msgid "(.*)"$', r"\1", line)
            buffer = ""
            mode = "msgid"
        elif line.startswith("msgstr "):
            mode = "msgstr"
            buffer = re.sub(r'^msgstr "(.*)"$', r"\1", line)
        elif line.startswith('"') and mode:
            chunk = line[1:-1]
            if mode == "msgid":
                msgid = (msgid or "") + chunk
            else:
                buffer += chunk
        elif not line and mode == "msgstr" and msgid is not None:
            entries[msgid] = buffer
            msgid, buffer, mode = None, "", None
    if mode == "msgstr" and msgid is not None:
        entries[msgid] = buffer
    entries.pop("", None)
    return entries


def test_every_string_has_a_german_translation() -> None:
    missing = [key for key, value in parse_catalog().items() if not value.strip()]
    assert missing == [], f"untranslated German strings: {missing}"


def test_no_translation_is_a_doubled_sentence() -> None:
    doubled = []
    for key, value in parse_catalog().items():
        half = len(value) // 2
        if len(value) > 20 and value[:half] == value[half:]:
            doubled.append(key)
    assert doubled == [], f"doubled German translations: {doubled}"


def test_placeholders_survive_translation() -> None:
    broken = []
    for key, value in parse_catalog().items():
        if set(re.findall(r"{(\w+)}", key)) != set(re.findall(r"{(\w+)}", value)):
            broken.append(key)
    assert broken == [], f"placeholders lost in translation: {broken}"


def test_unknown_languages_fall_back_to_english() -> None:
    assert normalise("fr") == "en"
    assert normalise(None) == "en"
    assert gettext_for("fr")("Who brings what") == "Who brings what"


def test_german_is_actually_compiled_and_loadable() -> None:
    assert "de" in SUPPORTED_LANGUAGES
    assert gettext_for("de")("Who brings what") == "Wer bringt was"
