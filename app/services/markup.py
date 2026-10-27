"""Markdown-lite rendering and @mention parsing.

Only a handful of inline constructs are allowed, and the result is sanitised
afterwards, so a message can never introduce script, styles, images or iframes
into somebody else's page. Rendering happens on display, from the stored raw
text, which keeps the database free of HTML.
"""

from __future__ import annotations

import re

import bleach
from markdown_it import MarkdownIt

ALLOWED_TAGS = ["p", "br", "strong", "em", "code", "ul", "ol", "li", "a", "span"]
ALLOWED_ATTRIBUTES = {
    "a": ["href", "rel", "target"],
    "span": ["class"],
}
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

EVERYONE = ("everyone", "all", "alle")

# Linkification is done afterwards by bleach, which also attaches the safe
# link attributes — enabling markdown-it's linkify as well would add a second
# dependency for the same job.
_md = (
    MarkdownIt("zero")
    .enable(["emphasis", "link", "list", "backticks", "newline"])
    .enable("paragraph")
)

# Grab a generous run after the @, then shorten it word by word until it
# matches a participant. That way "@Anna Maria" wins over "@Anna" when both
# exist, and trailing words or punctuation do not break the match.
MENTION_RE = re.compile(r"(?<![\w@])@([\w][\w .'-]{0,60})", re.UNICODE)


def find_mentions(text: str, known_names: list[str]) -> tuple[set[str], bool]:
    """Return the participant names mentioned, and whether @everyone was used."""
    everyone = False
    found: set[str] = set()
    lowered = {name.lower(): name for name in known_names}

    for candidate in MENTION_RE.findall(text or ""):
        words = candidate.split()
        for count in range(len(words), 0, -1):
            prefix = " ".join(words[:count]).strip(".,;:!?)'-").lower()
            if not prefix:
                continue
            if prefix in EVERYONE:
                everyone = True
                break
            if prefix in lowered:
                found.add(lowered[prefix])
                break
    return found, everyone


def render(text: str, known_names: list[str] | None = None) -> str:
    html = _md.render(text or "")
    if known_names:
        for name in sorted(known_names, key=len, reverse=True):
            pattern = re.compile(r"@" + re.escape(name) + r"\b", re.IGNORECASE)
            html = pattern.sub(f'<span class="mention">@{name}</span>', html)
    html = re.sub(
        r"@(everyone|all|alle)\b",
        lambda match: f'<span class="mention mention--all">@{match.group(1)}</span>',
        html,
        flags=re.IGNORECASE,
    )
    cleaned = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    linker = bleach.linkifier.Linker(
        callbacks=[
            lambda attrs, new: {
                **attrs,
                (None, "rel"): "noopener noreferrer nofollow",
                (None, "target"): "_blank",
            }
        ]
    )
    return linker.linkify(cleaned)
