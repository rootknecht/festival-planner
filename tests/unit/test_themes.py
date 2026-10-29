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

        # The card title must use the same face as that theme's wordmark, or
        # the overview advertises a look the festival does not have.
        wordmark = re.search(
            rf'\[data-theme="{name}"\]\s*\.titlebar__name\s*\{{[^}}]*?font-family:\s*([^;]+);',
            (THEME_DIR / f"{name}.css").read_text(), re.S,
        )
        if wordmark:
            assert card["font"].strip() == wordmark.group(1).strip(), (
                f"{name}: overview font {card['font']} != wordmark {wordmark.group(1)}"
            )


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


class _FakeUser:
    def __init__(self, override):
        self.theme_override = override


class _FakeState:
    def __init__(self, user):
        self.user = user


class _FakeRequest:
    def __init__(self, user=None):
        self.state = _FakeState(user)


class _FakeFestival:
    def __init__(self, theme):
        self.theme = theme


def test_a_user_follows_the_festival_by_default() -> None:
    """No override is the default, so an admin's choice per festival is what
    people see — including two festivals open in two tabs."""
    from app.templating import theme_for

    assert theme_for(_FakeRequest(_FakeUser(None)), _FakeFestival("punk")) == "punk"
    assert theme_for(_FakeRequest(None), _FakeFestival("metal")) == "metal"


def test_a_personal_override_wins_when_it_is_real() -> None:
    from app.templating import theme_for

    assert theme_for(_FakeRequest(_FakeUser("matrix")), _FakeFestival("punk")) == "matrix"


def test_a_stale_theme_name_falls_back_instead_of_404ing_the_stylesheet() -> None:
    """A theme name becomes a stylesheet path. A value left behind by a
    renamed or deleted theme must not reach the page: the browser would 404
    the file and render the app with no styling at all, which reads as a CSS
    bug rather than stale data."""
    from app.templating import theme_for

    assert theme_for(_FakeRequest(_FakeUser("blackopsone")), _FakeFestival("punk")) == "punk"
    assert theme_for(_FakeRequest(_FakeUser(None)), _FakeFestival("deleted")) in THEMES


def test_the_profile_offers_following_the_festival_as_the_first_choice() -> None:
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "app" / "templates" / "profile" / "index.html").read_text()
    first = page.index('name="theme_override"')
    assert 'value=""' in page[first:first + 400], "the empty 'follow the festival' option must come first"


def test_no_theme_rewrites_the_festival_name() -> None:
    """A theme chooses the face, the colour and the spacing — never the
    capitalisation of somebody's festival.

    Three themes set brand-transform: uppercase, so "RockHarz 2027" was
    displayed as "ROCKHARZ 2027" however carefully it had been typed. The
    overview cards never did this, which is how the inconsistency showed.
    Section headings may still shout: those are labels, not names.
    """
    import re

    for name in THEMES:
        css = (THEME_DIR / f"{name}.css").read_text()
        transform = re.search(r"--fp-brand-transform:\s*([^;]+);", css)
        if transform:
            assert transform.group(1).strip() == "none", (
                f"{name}.css rewrites the festival name as {transform.group(1).strip()}"
            )
        wordmark = re.search(
            rf'\[data-theme="{name}"\][^{{]*\.titlebar__name\s*\{{(.*?)\}}', css, re.S
        )
        if wordmark:
            assert "text-transform: uppercase" not in wordmark.group(1), (
                f"{name}.css uppercases the wordmark directly"
            )


def test_no_wordmark_asks_a_font_for_a_weight_it_does_not_have() -> None:
    """Synthetic bold quietly ruins a display face.

    The punk wordmark inherited font-weight: 700 from the shell while Rubik
    Dirt ships only 400. The browser obliged by smearing every stroke
    outward, which on a distressed face fills in the chipped holes that make
    it distressed — the same title, same font and same size looked visibly
    cleaner in the title bar than on the overview card, where it inherits
    400. Nothing errors; the font just stops looking like itself.
    """
    import re

    base = (THEME_DIR.parents[0] / "base.css").read_text()
    weights: dict[str, set[str]] = {}
    for block in re.findall(r"@font-face\s*\{(.*?)\}", base, re.S):
        family = re.search(r'font-family:\s*"([^"]+)"', block)
        weight = re.search(r"font-weight:\s*([^;]+);", block)
        if family and weight:
            weights.setdefault(family.group(1), set()).add(weight.group(1).strip())

    for name in THEMES:
        css = (THEME_DIR / f"{name}.css").read_text()
        rule = re.search(
            rf'\[data-theme="{name}"\][^{{]*\.titlebar__name\s*\{{(.*?)\}}', css, re.S
        )
        if not rule:
            continue
        family = re.search(r'font-family:\s*"([^"]+)"', rule.group(1))
        if not family or family.group(1) not in weights:
            continue
        available = weights[family.group(1)]
        asked = re.search(r"font-weight:\s*([^;]+);", rule.group(1))
        asked_value = asked.group(1).strip() if asked else "700"  # the shell default
        assert asked_value in available, (
            f"{name}: the wordmark asks {family.group(1)} for weight {asked_value}, "
            f"but only {sorted(available)} is shipped — the browser will fake it"
        )
