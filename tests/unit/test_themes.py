"""Every offered theme must have a stylesheet, and every stylesheet an offer.

A theme name goes straight into a <link href> path. When the two drift the
page does not break loudly: the browser 404s the stylesheet and renders the
app with no colours at all, which looks like a CSS bug rather than a missing
file. This test is the reason that cannot reach anyone.
"""

from pathlib import Path

from app.models.festival import THEMES

THEME_DIR = Path(__file__).resolve().parents[2] / "app" / "static" / "css" / "themes"


def test_every_offered_theme_has_a_stylesheet() -> None:
    missing = [name for name in THEMES if not (THEME_DIR / f"{name}.css").is_file()]
    assert not missing, f"offered in settings but no CSS file: {missing}"


def test_every_stylesheet_is_offered() -> None:
    on_disk = {p.stem for p in THEME_DIR.glob("*.css")}
    unlisted = sorted(on_disk - set(THEMES))
    assert not unlisted, f"stylesheet exists but nobody can pick it: {unlisted}"


def test_theme_names_are_safe_as_paths() -> None:
    """The name is interpolated into a URL; keep it to plain lowercase words."""
    for name in THEMES:
        assert name.isalnum() and name.islower(), name
