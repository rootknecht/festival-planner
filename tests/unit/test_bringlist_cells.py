"""The bring-list cell: what an empty cell shows.

Zero rendering as a dash in one place and as nothing in another was a defect
reported from production, so the formatting is pinned here rather than left to
the template.
"""

import re
from pathlib import Path

from app.services.bringlist import Cell

SHELL_CSS = Path(__file__).resolve().parents[2] / "app" / "static" / "css" / "shell.css"


def cell(amount: float, unit: str | None = None) -> Cell:
    return Cell(participant_id=1, amount=amount, unit=unit)


def test_zero_is_blank_everywhere_not_a_dash() -> None:
    """Zero means "brings nothing", so it shows nothing — no dash, no "0"."""
    assert cell(0.0, "L").display == ""
    assert cell(0.0).display == ""
    assert cell(0.0, "L").number == ""
    assert cell(0.0).number == ""


def test_an_empty_cell_offers_an_empty_field() -> None:
    """The input must not pre-fill a 0 the user has to delete first."""
    assert cell(0.0, "crates").number == ""


def test_a_whole_number_keeps_no_trailing_zero() -> None:
    assert cell(2.0, "L").number == "2"
    assert cell(2.0, "L").display == "2 L"
    assert cell(80.0).display == "80"


def test_a_fraction_survives() -> None:
    assert cell(2.5, "L").number == "2.5"
    assert cell(2.5, "L").display == "2.5 L"


def test_display_is_the_number_plus_its_unit() -> None:
    assert cell(3.0, "cans").display == "3 cans"
    assert cell(3.0, None).display == "3"


def test_the_empty_cell_keeps_a_clickable_height() -> None:
    """An empty summary is 0px tall without min-height, so it cannot be clicked.

    Measured in a browser: with every cell in a row cleared, the click boxes
    were 35x0. It hides itself whenever a sibling cell still has text, because
    that props the table row up — so this rule is pinned rather than trusted.
    """
    css = SHELL_CSS.read_text(encoding="utf-8")
    match = re.search(r"\.cell__value\s*\{([^}]*)\}", css)
    assert match is not None, ".cell__value rule is gone from shell.css"
    block = match.group(1)
    assert "min-height" in block, ".cell__value must keep a min-height when empty"
    assert "min-width" in block, ".cell__value must keep a min-width when empty"
