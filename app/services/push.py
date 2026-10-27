"""Web Push delivery.

Push is a courtesy, never the source of truth: the in-app red dots are. A
failed push must therefore never break the request that triggered it, and a
subscription the push service rejects as gone is deleted rather than retried.

On iPhones push only works once the user has added the app to the home screen,
and Apple drops subscriptions unpredictably — which is exactly why the dots
stay authoritative.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.config import Settings, get_settings
from app.models.push import PushSubscription
from app.models.user import utcnow

logger = logging.getLogger(__name__)

# Small pool: delivery must not hold up the HTTP response.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webpush")

GONE_STATUS = (404, 410)


def enabled(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.vapid_private and settings.vapid_public_key)


def subscriptions_for(db: DbSession, user_id: int) -> list[PushSubscription]:
    return list(
        db.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id))
    )


def _deliver(subscription_info: dict, payload: str, settings: Settings) -> int | None:
    """Returns the HTTP status that should cause deletion, or None."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=settings.vapid_private,
            vapid_claims={"sub": settings.vapid_subject or "mailto:admin@example.org"},
            timeout=10,
        )
        return None
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in GONE_STATUS:
            return status
        logger.warning("web push failed: %s", exc)
        return None
    except Exception as exc:  # pragma: no cover - network edge cases
        logger.warning("web push error: %s", exc)
        return None


def send_to_user(
    db: DbSession,
    user_id: int,
    *,
    title: str,
    body: str,
    url: str,
    settings: Settings | None = None,
) -> int:
    """Queue a push to every device of one user. Returns how many were queued."""
    settings = settings or get_settings()
    if not enabled(settings):
        return 0

    payload = json.dumps({"title": title, "body": body, "url": url})

    # Collect and commit FIRST, dispatch afterwards. If a delivery deletes a
    # dead subscription in its own session while this one still holds pending
    # changes for that row, the commit fails with a stale-data error — in
    # production that is a race, here it was reproducible.
    targets: list[tuple[int, dict]] = []
    for subscription in subscriptions_for(db, user_id):
        targets.append(
            (
                subscription.id,
                {
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
            )
        )
        subscription.last_used_at = utcnow()
    db.commit()

    queued = 0
    for subscription_id, info in targets:

        def task(info=info, subscription_id=subscription_id) -> None:
            from app.db import get_session_factory

            gone = _deliver(info, payload, settings)
            if gone is None:
                return
            # The push service says this device is gone for good.
            session = get_session_factory()()
            try:
                stale = session.get(PushSubscription, subscription_id)
                if stale is not None:
                    session.delete(stale)
                    session.commit()
            finally:
                session.close()

        if settings.environment == "test":
            # Deterministic in tests: no thread timing, no shared executor
            # state leaking between test cases.
            task()
        else:
            _executor.submit(task)
        queued += 1
    return queued


def prune_gone(db: DbSession, endpoint: str) -> bool:
    subscription = db.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    )
    if subscription is None:
        return False
    db.delete(subscription)
    db.commit()
    return True
