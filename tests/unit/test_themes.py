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


def _theme_vars(css: str) -> dict[str, str]:
    import re

    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"--(fp-[a-z0-9-]+):\s*([^;]+);", css)}


def test_the_overview_palette_matches_the_themes_it_copies() -> None:
    """The festival list draws each card in its festival's own colours.

    Those values are duplicated into shell.css because the project has no
    build step and the CSP forbids inline styles. Duplication is acceptable
    only while something checks it, which is this test: it re-reads both
    sides and fails the moment a theme is retuned and the overview is not.
    """
    import re

    shell = (THEME_DIR.parents[0] / "shell.css").read_text()
    for name in THEMES:
        theme = _theme_vars((THEME_DIR / f"{name}.css").read_text())
        block = re.search(rf"\.cardtheme--{name}\s*\{{(.*?)\}}", shell, re.S)
        assert block, f"the overview has no card rule for the {name} theme"
        card = dict(re.findall(r"--card-([a-z]+):\s*([^;]+);", block.group(1)))
        assert card["accent"].strip() == theme["fp-accent"], (
            f"{name}: overview accent {card['accent']} != theme {theme['fp-accent']}"
        )
        assert card["bg"].strip() == theme["fp-surface"], f"{name}: surface drifted"
        assert card["fg"].strip() == theme["fp-fg"], f"{name}: text colour drifted"


def test_the_fonts_are_declared_where_every_page_can_use_them() -> None:
    """The overview renders each festival's title in that theme's face while
    itself being drawn in one theme, so @font-face must live in base.css. A
    declaration is free: the browser fetches a font only when it is used."""
    base = (THEME_DIR.parents[0] / "base.css").read_text()
    assert "@font-face" in base
    for name in THEMES:
        assert "@font-face" not in (THEME_DIR / f"{name}.css").read_text(), (
            f"{name}.css declares a font — move it to base.css or the overview "
            "cannot render it"
        )
