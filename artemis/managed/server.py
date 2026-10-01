"""The admin server's API surface in managed deployments.

The console has no accounts: whoever reaches the port is the operator, and
many routes change the host (credentials, adb server, emulator, restart).
A managed server listens on loopback inside the user's runtime pod, and on
top of that every request must carry the per-launch API token and hit one
of the few routes the runtime uses to run and watch a task.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from typing import Any

from artemis.managed.settings import MANAGED_PROFILE, ManagedSettings

__all__ = ["ManagedApiMiddleware", "check_run_request", "install"]

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
#: Session ids are UUIDs, which also keeps ``/api/stream/device-live`` out.
SESSION_ID = re.compile(_UUID)
ALLOWED_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"/api/run")),
    ("POST", re.compile(r"/api/stop")),
    ("GET", re.compile(rf"/api/stream/{_UUID}")),
    ("GET", re.compile(rf"/api/sessions/{_UUID}")),
    ("GET", re.compile(rf"/api/sessions/{_UUID}/steps")),
    ("GET", re.compile(r"/api/images/[A-Za-z0-9._-]+")),
)
#: ``/api/run`` fields a managed runtime may set; the rest would install
#: APKs, switch to Pro tuning or lock apps.
RUN_FIELDS = frozenset({"goal", "profile", "device_serial", "session_id", "ingress"})


def is_allowed(method: str, path: str) -> bool:
    return any(method == m and pattern.fullmatch(path) for m, pattern in ALLOWED_ROUTES)


def check_run_request(fields: dict[str, Any]) -> str | None:
    """Why a managed server refuses this ``/api/run`` body, or ``None``."""
    extra = sorted(k for k, v in fields.items() if v is not None and k not in RUN_FIELDS)
    if extra:
        return f"Not available in managed mode: {', '.join(extra)}"
    if (fields.get("profile") or MANAGED_PROFILE).lower() != MANAGED_PROFILE:
        return f"Only the '{MANAGED_PROFILE}' profile runs in managed mode"
    if not fields.get("goal"):
        return "A goal is required"
    if not SESSION_ID.fullmatch(str(fields.get("session_id") or "")):
        return "A UUID session_id is required"
    return None


class ManagedApiMiddleware:
    """Pure ASGI, so no route or protocol can slip past it."""

    def __init__(self, app: Any, *, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        authorization = b""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                authorization = value
        if not hmac.compare_digest(authorization, self._expected):
            await _reply(send, 401, "Unauthorized")
            return
        if not is_allowed(scope["method"], scope["path"]):
            await _reply(send, 404, "Not found")
            return
        await self.app(scope, receive, send)


async def _reply(send: Any, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def install(app: Any) -> None:
    """Lock the admin server down; raises if a required setting is missing."""
    settings = ManagedSettings.from_env()
    settings.check()
    # The control plane puts idle phones to sleep; do not hold them awake.
    os.environ["ARTEMIS_KEEP_DEVICE_AWAKE"] = "false"
    app.add_middleware(ManagedApiMiddleware, token=settings.api_token)
