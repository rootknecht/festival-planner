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


def test_every_font_a_theme_asks_for_is_present() -> None:
    """A missing woff2 does not error — it silently falls back to a system
    face, so the theme looks subtly wrong rather than broken, and nobody
    notices until someone says "that doesn't look like the poster"."""
    import re

    font_dir = THEME_DIR.parents[1] / "fonts"
    missing = []
    for sheet in THEME_DIR.glob("*.css"):
        for url in re.findall(r'url\("(/static/fonts/[^"]+)"\)', sheet.read_text()):
            if not (font_dir / url.rsplit("/", 1)[1]).is_file():
                missing.append(f"{sheet.name} -> {url}")
    assert not missing, f"stylesheet references a font that is not shipped: {missing}"


def test_every_shipped_font_carries_its_licence() -> None:
    """A font must travel with its own licence text, and with the RIGHT one.

    Special Elite is Apache 2.0, not OFL — it lives in google/fonts' apache/
    tree. Downloading an OFL.txt for it silently fetched a 404 HTML page, so
    this checks the CONTENT names a real licence rather than trusting the
    filename. This repo is public; shipping a font under a licence it is not
    under is the kind of mistake nobody notices for years.
    """
    font_dir = THEME_DIR.parents[1] / "fonts"
    known = ("SIL OPEN FONT LICENSE", "APACHE LICENSE")
    for font in font_dir.glob("*.woff2"):
        family = font.stem.split("-")[0]
        candidates = [font_dir / f"{family}-OFL.txt", font_dir / f"{family}-LICENSE.txt"]
        found = [c for c in candidates if c.is_file()]
        assert found, f"{font.name} ships without a licence file"
        text = found[0].read_text().upper()
        assert any(k in text for k in known), (
            f"{found[0].name} does not contain a recognisable licence — "
            "a failed download leaves an HTML error page behind"
        )
