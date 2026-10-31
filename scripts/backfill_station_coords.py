#!/usr/bin/env python
"""Geocode train stations that predate the map feature.

Stations are placed when a train is SAVED, which leaves every journey entered
before v0.16.0 without coordinates — and a journey with no coordinates draws
no line, which is exactly what it looked like in production: "the train route
does not show at all".

Idempotent: a station that already has a pin is skipped, and every lookup goes
through the geocode cache, so a second run makes no network requests at all.
Run inside the app's environment:

    /opt/festival-planner/.venv/bin/python scripts/backfill_station_coords.py

Add --dry-run to see what it would ask for without writing anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models.transport import Train  # noqa: E402
from app.services.geocode import geocode  # noqa: E402


def place(db, name: str, settings, dry_run: bool) -> tuple[float | None, float | None]:
    """Same two-step as the route: the name as written, then as a station."""
    name = (name or "").strip()
    if not name:
        return None, None
    if dry_run:
        print(f"    would look up {name!r}")
        return None, None
    lat, lon = geocode(db, name, settings)
    if lat is None:
        lat, lon = geocode(db, f"{name} station", settings)
    return lat, lon


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    placed = skipped = unplaceable = 0

    with session_scope() as db:
        for train in db.scalars(select(Train)):
            print(f"train {train.id}: {train.number or '(no number)'}")
            for attribute, station in (
                ("departure", train.departure_station),
                ("arrival", train.arrival_station),
            ):
                if not (station or "").strip():
                    continue
                if getattr(train, f"{attribute}_lat") is not None:
                    skipped += 1
                    continue
                lat, lon = place(db, station, settings, args.dry_run)
                if args.dry_run:
                    continue
                setattr(train, f"{attribute}_lat", lat)
                setattr(train, f"{attribute}_lon", lon)
                if lat is None:
                    print(f"    {attribute}: {station!r} could not be placed")
                    unplaceable += 1
                else:
                    print(f"    {attribute}: {station!r} -> {lat},{lon}")
                    placed += 1

            for change in train.changes:
                if change.lat is not None or not (change.station or "").strip():
                    skipped += 1
                    continue
                lat, lon = place(db, change.station, settings, args.dry_run)
                if args.dry_run:
                    continue
                change.lat, change.lon = lat, lon
                if lat is None:
                    print(f"    change: {change.station!r} could not be placed")
                    unplaceable += 1
                else:
                    print(f"    change: {change.station!r} -> {lat},{lon}")
                    placed += 1

        if args.dry_run:
            db.rollback()

    print(f"\nplaced {placed}, already placed {skipped}, could not place {unplaceable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
