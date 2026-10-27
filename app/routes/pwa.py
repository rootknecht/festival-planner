"""PWA plumbing: manifest, service worker, push subscription endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.deps import get_db, require_user
from app.models.push import PushSubscription
from app.models.user import User
from app.services import push
from app.templating import render

router = APIRouter(tags=["pwa"])

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def _settings_for(request: Request):
    return getattr(request.app.state, "settings", None) or get_settings()


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "Festival Planner",
            "short_name": "Festival",
            "description": "Plan a festival trip with your friends.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#0c0c0e",
            "theme_color": "#0c0c0e",
            "lang": "en",
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {
                    "src": "/static/icons/icon-maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        },
        media_type="application/manifest+json",
    )


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    """Served from the root so its scope covers the whole app.

    A service worker at /static/sw.js could only control /static/.
    """
    return FileResponse(
        STATIC_DIR / "js" / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@router.get("/offline", include_in_schema=False)
async def offline(request: Request) -> Response:
    return render(request, "offline.html", {})


@router.get("/push/key")
async def push_key(request: Request, user: User = Depends(require_user)) -> JSONResponse:
    settings = _settings_for(request)
    return JSONResponse(
        {"enabled": push.enabled(settings), "key": settings.vapid_public_key or ""}
    )


@router.post("/push/subscribe")
async def subscribe(
    request: Request,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> JSONResponse:
    data = await request.json()
    endpoint = (data or {}).get("endpoint")
    keys = (data or {}).get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return JSONResponse({"detail": "incomplete subscription"}, status_code=400)

    existing = db.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    )
    if existing is not None:
        # Re-subscribing on the same device must not create a duplicate, and a
        # shared browser may hand the endpoint to a different account.
        existing.user_id = user.id
        existing.p256dh = keys["p256dh"]
        existing.auth = keys["auth"]
    else:
        db.add(
            PushSubscription(
                user_id=user.id,
                endpoint=endpoint,
                p256dh=keys["p256dh"],
                auth=keys["auth"],
            )
        )
    db.commit()
    return JSONResponse({"status": "subscribed"})


@router.post("/push/unsubscribe")
async def unsubscribe(
    request: Request,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> JSONResponse:
    data = await request.json()
    endpoint = (data or {}).get("endpoint", "")
    subscription = db.scalar(
        select(PushSubscription).where(
            PushSubscription.endpoint == endpoint, PushSubscription.user_id == user.id
        )
    )
    if subscription is not None:
        db.delete(subscription)
        db.commit()
    return JSONResponse({"status": "unsubscribed"})
