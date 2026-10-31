"""Which public identity a mail goes out under.

One instance can serve several domains (separate proxy hosts, same backend),
and a festival may belong to one of them: an invitation to a punk weekend
should arrive from the punk domain and link there, not from whichever domain
happens to be the instance default.

A brand is therefore a pair — the From address and the link base — kept
together on purpose. A mail whose sender and link disagree looks like phishing
to a human and scores like phishing at a filter, so these two must never be
chosen independently.

Everything here resolves against an ALLOWLIST (Settings.mail_brands). The
value ends up in a mail header and at the front of a one-time link, so free
text is out of the question: an unknown domain always falls back to the
instance default rather than being trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings


@dataclass(frozen=True)
class Brand:
    #: Configured domain, or "" for the instance default.
    domain: str
    #: From address for mail sent under this brand ("" = settings.smtp_from).
    sender: str
    #: Link base, without a trailing slash.
    base_url: str


def _instance_brand(settings: Settings) -> Brand:
    return Brand(domain="", sender=settings.smtp_from, base_url=settings.base_url.rstrip("/"))


def brands(settings: Settings) -> dict[str, Brand]:
    """Every selectable brand, keyed by domain, in configured order."""
    return {
        domain: Brand(domain=domain, sender=sender, base_url=f"https://{domain}")
        for domain, sender in settings.brand_senders.items()
    }


def is_known(settings: Settings, domain: str) -> bool:
    return domain.strip().lower() in settings.brand_senders


def brand_for(settings: Settings, festival: Any | None) -> Brand:
    """The brand a festival sends under; the instance default if unset.

    Reads defensively: a festival whose stored domain was removed from the
    configuration keeps working on the default instead of sending from a
    domain this instance can no longer authenticate.
    """
    domain = (getattr(festival, "mail_domain", "") or "").strip().lower()
    return brands(settings).get(domain) or _instance_brand(settings)


def brand_for_host(settings: Settings, host: str | None) -> Brand:
    """The brand matching the domain a request arrived on.

    For account-level mail — the self-service password reset — there is no
    festival to ask: the address may belong to someone in festivals on several
    domains, or in none, and the route must not reveal which. The domain the
    person is actually looking at is the honest answer.

    SECURITY: Host is attacker-controlled. It is matched against the allowlist
    and anything else falls back to the instance default, so a forged header
    can neither redirect a reset link nor forge the sender of one.
    """
    name = (host or "").split(",")[0].strip().lower()
    name = name.rsplit("@", 1)[-1]  # defensive: userinfo never belongs here
    if name.startswith("[") and "]" in name:  # IPv6 literal
        name = name[: name.index("]") + 1]
    elif ":" in name:
        name = name.split(":", 1)[0]
    return brands(settings).get(name) or _instance_brand(settings)
