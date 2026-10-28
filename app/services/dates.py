"""Human date formatting, per language.

English wants "12th July 2027" with an ordinal; German wants "12. Juli 2027"
with a plain full stop. Babel's own format_date gives neither exactly — the
English locale produces "12 July 2027" — so the month name comes from Babel
(which knows every language) and the day is assembled here.
"""

from __future__ import annotations

import datetime as dt

from babel.dates import format_date as babel_format_date


def _english_ordinal(day: int) -> str:
    # 11th, 12th, 13th are the exceptions that a naive %10 rule gets wrong.
    if 11 <= day % 100 <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"


def format_long_date(value: dt.date | None, language: str = "en") -> str:
    """"12th July 2027" in English, "12. Juli 2027" in German."""
    if value is None:
        return ""
    month = babel_format_date(value, format="LLLL", locale=language or "en")
    if (language or "en").startswith("de"):
        return f"{value.day}. {month} {value.year}"
    return f"{_english_ordinal(value.day)} {month} {value.year}"


def format_date_range(
    start: dt.date | None, end: dt.date | None, language: str = "en"
) -> str:
    """A range, collapsing what the two ends share.

    "12th-15th July 2027" rather than repeating the month and year, because
    this sits in the title bar on a phone where every character counts.
    """
    if start is None:
        return format_long_date(end, language)
    if end is None or end == start:
        return format_long_date(start, language)

    german = (language or "en").startswith("de")
    if start.year == end.year and start.month == end.month:
        first = f"{start.day}." if german else _english_ordinal(start.day)
        return f"{first}–{format_long_date(end, language)}"
    if start.year == end.year:
        month = babel_format_date(start, format="LLLL", locale=language or "en")
        first = f"{start.day}. {month}" if german else f"{_english_ordinal(start.day)} {month}"
        return f"{first} – {format_long_date(end, language)}"
    return f"{format_long_date(start, language)} – {format_long_date(end, language)}"
