"""Translations.

Catalogs live in ``app/i18n/<lang>/LC_MESSAGES/messages.po`` and are compiled
to ``.mo`` by ``scripts/compile-i18n.sh`` (the installer runs it too). English
is the source language and the fallback for any string a catalog is missing,
so an incomplete translation degrades to English instead of showing a key.
"""

from __future__ import annotations

import gettext
from pathlib import Path

I18N_DIR = Path(__file__).parent / "i18n"
DOMAIN = "messages"
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES: dict[str, str] = {"en": "English", "de": "Deutsch"}

_cache: dict[str, gettext.NullTranslations] = {}


def N_(message: str) -> str:
    """Mark a string for extraction without translating it here.

    Used for strings stored in dictionaries and in the database, which are
    translated later, in the reader's language.
    """
    return message


def normalise(language: str | None) -> str:
    if not language:
        return DEFAULT_LANGUAGE
    code = language.split("-")[0].split("_")[0].strip().lower()
    return code if code in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def get_translations(language: str | None) -> gettext.NullTranslations:
    code = normalise(language)
    if code not in _cache:
        _cache[code] = gettext.translation(
            DOMAIN,
            localedir=str(I18N_DIR),
            languages=[code],
            fallback=True,
        )
    return _cache[code]


def gettext_for(language: str | None):
    return get_translations(language).gettext


def clear_cache() -> None:
    _cache.clear()
