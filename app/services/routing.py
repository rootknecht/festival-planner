"""Road geometry for a car's line on the map, via openrouteservice.

Why not the public OSRM demo server: its terms forbid use by applications.
openrouteservice publishes a free tier that permits it (2000 requests/day),
so that is what this talks to — from the SERVER, never the browser, so the
key is never shipped to a page and no CSP origin has to be opened.

Three rules keep this cheap and safe to leave switched on:

* No key, no request. The feature is off by default and the map falls back to
  the straight dashed line it drew before, which is a sketch and looks like
  one. A planning tool nobody navigates by must not break because a third
  party is down.
* A geometry is fetched once per route VERSION. The cache key is the rounded
  coordinate list, so editing a stop earns a new lookup and a page view earns
  none. Without this, every render of a festival with six cars would be six
  requests and the daily allowance would be gone by lunch.
* A failure is cached too, briefly. Otherwise one unroutable car (a stop in
  the middle of a lake) retries on every single page view.

Nothing here sends an address: only coordinates that are already on the map
in front of the user, of public meeting places and departure towns.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

import httpx
from sqlalchemy.orm import Session as DbSession

from app.config import Settings, get_settings
from app.models.transport import RouteCache
from app.models.user import utcnow

logger = logging.getLogger(__name__)

#: A successful geometry is kept until the coordinates change, i.e. forever.
#: A failure is retried after this, because "unroutable" is often temporary
#: (a service outage, a quota wall) rather than a property of the route.
FAILURE_RETRY = dt.timedelta(hours=6)

#: Coordinates are rounded before hashing so that a re-geocode returning
#: 53.5511 vs 53.55110001 does not count as a different route.
_PRECISION = 5

#: openrouteservice refuses more than 50 waypoints on the free tier, and a
#: line with that many bends is unreadable anyway.
MAX_WAYPOINTS = 25


def _key(profile: str, coords: list[tuple[float, float]], radius: float) -> str:
    """Cache key. The snap radius belongs in it.

    Raising the radius turns a previously impossible route into a possible
    one, so a key without it would keep serving the remembered failure until
    the retry window expired — a config change that appears to do nothing.
    """
    rounded = [(round(lat, _PRECISION), round(lon, _PRECISION)) for lat, lon in coords]
    raw = f"{profile}|{radius:.0f}|" + ";".join(f"{lat},{lon}" for lat, lon in rounded)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fetch(
    coords: list[tuple[float, float]], settings: Settings | None = None
) -> list[list[float]] | None:
    """Ask openrouteservice for the driving geometry. Returns [[lat, lon], ...].

    Returns None on anything unexpected — no key, a refusal, a timeout, a
    shape that is not what the API documents. The caller draws a straight
    line in that case, which is what it did before this existed.
    """
    settings = settings or get_settings()
    if not settings.ors_api_key:
        return None
    try:
        response = httpx.post(
            f"{settings.ors_url}/{settings.ors_profile}/geojson",
            # The API takes lon,lat — the opposite order to everything else in
            # this codebase, which is a classic way to end up routing through
            # the Indian Ocean.
            #
            # `radiuses` is not optional in practice. A FESTIVAL SITE IS A
            # FIELD: Rock Harz geocodes to a point with no mapped road within
            # the API's default 350 m, so every line through it came back 404
            # "could not find routable point" while a city-to-city line
            # worked. One number per waypoint; the API wants the list.
            json={
                "coordinates": [[lon, lat] for lat, lon in coords],
                "radiuses": [settings.ors_snap_radius] * len(coords),
            },
            headers={
                "Authorization": settings.ors_api_key,
                "Content-Type": "application/json",
            },
            timeout=settings.ors_timeout,
        )
        response.raise_for_status()
        payload = response.json()
        line = payload["features"][0]["geometry"]["coordinates"]
    except httpx.HTTPStatusError as exc:
        # The STATUS alone sent a debugging round down the wrong path: a 404
        # here is not "endpoint missing", it is the API's way of saying one
        # coordinate has no road near it, and the reason is in the body.
        detail = ""
        try:
            error = exc.response.json().get("error", {})
            detail = f" | code {error.get('code')}: {error.get('message')}"
        except (ValueError, AttributeError):
            detail = f" | body {exc.response.text[:200]}"
        logger.warning("routing %d waypoints failed: %s%s", len(coords), exc, detail)
        return None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("routing %d waypoints failed: %s", len(coords), exc)
        return None
    try:
        return [[float(lat), float(lon)] for lon, lat in line]
    except (TypeError, ValueError) as exc:
        logger.warning("routing returned an unusable geometry: %s", exc)
        return None


def road_route(
    db: DbSession,
    coords: list[tuple[float, float]],
    settings: Settings | None = None,
) -> list[list[float]] | None:
    """Cached road geometry for a list of waypoints, or None to draw straight."""
    settings = settings or get_settings()
    if not settings.ors_api_key or len(coords) < 2:
        return None
    if len(coords) > MAX_WAYPOINTS:
        logger.info("not routing %d waypoints, over the limit", len(coords))
        return None

    key = _key(settings.ors_profile, coords, settings.ors_snap_radius)
    cached = db.get(RouteCache, key)
    if cached is not None:
        if cached.geometry:
            try:
                return json.loads(cached.geometry)
            except ValueError:
                pass  # Corrupt row: fall through and ask again.
        else:
            # SQLite returns a NAIVE datetime even though it was written as
            # aware, so comparing it directly raises. Same fix as the invite
            # and reset expiry checks.
            fetched = cached.fetched_at
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=dt.UTC)
            if utcnow() - fetched < FAILURE_RETRY:
                return None

    line = fetch(coords, settings)
    payload = json.dumps(line) if line else ""
    if cached is None:
        db.add(
            RouteCache(
                route_hash=key,
                profile=settings.ors_profile,
                waypoints=len(coords),
                geometry=payload,
                fetched_at=utcnow(),
            )
        )
    else:
        cached.geometry = payload
        cached.fetched_at = utcnow()
    db.commit()
    return line
