"""Numeric dates must follow the reader's language.

"05/03/27" is the 5th of March to a German and the 3rd of May to an English
reader. The same string meaning two different days is not a cosmetic issue
on a page whose whole purpose is agreeing when to leave, so the order is
asserted explicitly rather than assumed.
"""

import datetime as dt

from app.services.dates import format_numeric_date, format_numeric_datetime

DAY = dt.date(2027, 7, 28)
MOMENT = dt.datetime(2027, 7, 28, 9, 14)


def test_english_puts_the_month_first() -> None:
    assert format_numeric_date(DAY, "en") == "07/28/27"


def test_german_puts_the_day_first() -> None:
    assert format_numeric_date(DAY, "de") == "28/07/27"


def test_an_ambiguous_looking_date_differs_between_the_two() -> None:
    day = dt.date(2027, 3, 5)
    assert format_numeric_date(day, "en") == "03/05/27"
    assert format_numeric_date(day, "de") == "05/03/27"


def test_an_unknown_language_reads_as_english() -> None:
    assert format_numeric_date(DAY, "fr") == "07/28/27"
    assert format_numeric_date(DAY, "") == "07/28/27"
    assert format_numeric_date(DAY, None) == "07/28/27"


def test_a_missing_date_renders_as_nothing_not_as_none() -> None:
    assert format_numeric_date(None, "de") == ""
    assert format_numeric_datetime(None, "en") == ""


def test_the_clock_stays_24_hour_in_both_languages() -> None:
    """am/pm invites exactly the midnight ambiguity a travel plan cannot afford."""
    assert format_numeric_datetime(MOMENT, "en") == "07/28/27 09:14"
    assert format_numeric_datetime(MOMENT, "de") == "28/07/27 09:14"


def test_the_language_tag_may_carry_a_region() -> None:
    assert format_numeric_date(DAY, "de-DE") == "28/07/27"
    assert format_numeric_date(DAY, "en-GB") == "07/28/27"
