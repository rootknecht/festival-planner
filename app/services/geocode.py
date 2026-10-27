"""Address lookup through Nominatim, cached and rate limited.

The public Nominatim service has a usage policy: identify yourself, do not
hammer it, cache results. All three are implemented here. Lookups happen only
when a user explicitly saves an address, never on page render, and a failure
is not an error — the address is simply stored without coordinates and the map
shows the rest.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time

import httpx
from sqlalchemy.orm import Session as DbSession

from app.config import Settings, get_settings
from app.models.transport import GeocodeCache
from app.models.user import utcnow

logger = logging.getLogger(__name__)

_last_call = 0.0
_lock = threading.Lock()


def _key(query: str) -> str:
    return hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()


def _throttle(min_interval: float) -> None:
    global _last_call
    with _lock:
        wait = min_interval - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def lookup(query: str, settings: Settings | None = None) -> tuple[float, float, str] | None:
    """Ask Nominatim. Returns (lat, lon, display_name) or None."""
    settings = settings or get_settings()
    _throttle(settings.nominatim_min_interval)
    try:
        response = httpx.get(
            settings.nominatim_url,
            params={"q": query, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": settings.nominatim_user_agent},
            timeout=10.0,
        )
        response.raise_for_status()
        results = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("geocoding %r failed: %s", query, exc)
        return None
    if not results:
        return None
    first = results[0]
    try:
        return float(first["lat"]), float(first["lon"]), first.get("display_name", query)
    except (KeyError, TypeError, ValueError):
        return None


def geocode(
    db: DbSession, query: str, settings: Settings | None = None
) -> tuple[float | None, float | None]:
    """Cached geocoding. A cached miss is remembered too, so a typo is not
    retried against the public service on every save."""
    query = (query or "").strip()
    if not query:
        return None, None

    key = _key(query)
    cached = db.get(GeocodeCache, key)
    if cached is not None:
        return cached.lat, cached.lon

    found = lookup(query, settings)
    lat, lon, display = (found or (None, None, None))
    db.add(
        GeocodeCache(
            query_hash=key,
            query=query[:500],
            lat=lat,
            lon=lon,
            display_name=display,
            fetched_at=utcnow(),
        )
    )
    db.commit()
    return lat, lon
