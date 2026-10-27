"""WebSocket hub: one channel per festival.

Used for chat messages and for telling open pages that a shared table changed.
Membership is checked once, when the socket connects — a socket that is not a
participant of that festival is refused before it joins the channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from starlette.websockets import WebSocket, WebSocketState

logger = logging.getLogger(__name__)


class Hub:
    def __init__(self) -> None:
        self._channels: dict[int, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def join(self, festival_id: int, socket: WebSocket) -> None:
        async with self._lock:
            self._channels[festival_id].add(socket)

    async def leave(self, festival_id: int, socket: WebSocket) -> None:
        async with self._lock:
            self._channels[festival_id].discard(socket)
            if not self._channels[festival_id]:
                self._channels.pop(festival_id, None)

    def connection_count(self, festival_id: int) -> int:
        return len(self._channels.get(festival_id, ()))

    async def broadcast(self, festival_id: int, payload: dict) -> int:
        """Send to every open socket of one festival. Never raises."""
        message = json.dumps(payload)
        dead: list[WebSocket] = []
        sent = 0
        for socket in list(self._channels.get(festival_id, ())):
            if socket.client_state is not WebSocketState.CONNECTED:
                dead.append(socket)
                continue
            try:
                await socket.send_text(message)
                sent += 1
            except (RuntimeError, OSError) as exc:
                logger.debug("dropping a dead socket: %s", exc)
                dead.append(socket)
        for socket in dead:
            await self.leave(festival_id, socket)
        return sent


hub = Hub()


def notify_change(festival_id: int, tool: str) -> None:
    """Tell open pages that a tool's data changed, from sync code.

    Fire-and-forget: a missing event loop (tests, CLI) must never break the
    request that triggered it.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(hub.broadcast(festival_id, {"type": "changed", "tool": tool}))
