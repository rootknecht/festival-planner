"""Every icon name a template asks for must actually draw something.

An unknown name falls off the end of the macro's elif chain and renders an
empty <svg>: no error, no warning, just a blank space where the icon should
be. That is how "beer" and then "beerglass" both shipped invisible — the
page looked fine to the server and fine to the tests, and only a human
looking at the screen could tell.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"
ICONS = TEMPLATES / "_icons.html"

#: Names the macro knows how to draw.
DEFINED = set(re.findall(r'name == "([a-z0-9_-]+)"', ICONS.read_text()))

#: Names the templates ask for, with the file that asks, for a useful failure.
USED = sorted(
    {
        (match.group(1), path.relative_to(TEMPLATES).as_posix())
        for path in TEMPLATES.rglob("*.html")
        for match in re.finditer(r'icon\(\s*["\']([a-z0-9_-]+)["\']', path.read_text())
    }
)


def test_the_icon_set_is_not_empty() -> None:
    """Guards the guard: a broken regex here would pass everything below."""
    assert len(DEFINED) > 5
    assert len(USED) > 5


@pytest.mark.parametrize(("name", "template"), USED)
def test_every_icon_used_in_a_template_is_drawn(name: str, template: str) -> None:
    assert name in DEFINED, (
        f"{template} asks for icon({name!r}), which the macro does not draw — "
        f"it would render an empty svg. Known: {sorted(DEFINED)}"
    )


@pytest.mark.parametrize("name", sorted(DEFINED))
def test_every_defined_icon_has_actual_geometry(name: str) -> None:
    """A branch that matches but draws nothing is just as invisible."""
    body = ICONS.read_text().split(f'name == "{name}"')[1]
    body = re.split(r"\{%-? *elif|\{%-? *else|\{%-? *endif", body)[0]
    assert re.search(r"<(path|circle|rect|line|polyline|polygon)\b", body), (
        f"icon({name!r}) has a branch but draws no shapes"
    )


def test_no_icon_is_drawn_as_a_font_character() -> None:
    """Icons must be inline SVG, never a Unicode symbol.

    The sign-out button used U+23FB POWER SYMBOL. Android ships no font
    containing that glyph, so on a phone the button was simply blank — not
    broken, not an error, just invisible, and only on some devices. Every
    symbol character below was in use and every one of them is a gamble on
    the device's font coverage.
    """
    import re
    from pathlib import Path

    templates = Path(__file__).resolve().parents[2] / "app" / "templates"
    # Decorative symbols. Ordinary punctuation entities (&amp; &nbsp; &quot;)
    # and the typographic dashes in prose are fine; these are PICTURES.
    symbols = re.compile(r"&#(816[0-9]{2}|9[0-9]{3}|1[0-9]{4}|x2[0-9A-Fa-f]{3});")
    offenders = []
    for path in templates.rglob("*.html"):
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            if symbols.search(line):
                offenders.append(f"{path.name}:{line_no}: {line.strip()[:70]}")
    assert not offenders, (
        "font-glyph icons found — use icon('name') from _icons.html instead:\n"
        + "\n".join(offenders)
    )
