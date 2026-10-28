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
