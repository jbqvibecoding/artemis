"""Asks the control plane for the user's decision.

Same protocol as the runtime it runs next to: create an approval, then
long-poll it until the user decides or it expires. Anything but an explicit
``approved`` is a refusal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import time
from typing import Any

import httpx

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["ApprovalClient", "FINAL_STATUSES"]

FINAL_STATUSES = frozenset({"approved", "denied", "expired"})
POLL_SECONDS = 25.0
#: Extra time past the control plane's expiry before giving up on our side.
GRACE_SECONDS = 30.0
RETRY_DELAY = 2.0


class ApprovalClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_seconds: float = POLL_SECONDS,
        retry_delay: float = RETRY_DELAY,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport
        self._poll = poll_seconds
        self._retry_delay = retry_delay
        self._clock = clock
        self._sleep = sleep

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=10.0,
            transport=self._transport,
        )

    async def request(
        self,
        message: str,
        *,
        context: str,
        device_id: str | None = None,
        package: str | None = None,
        app_name: str | None = None,
        screenshot: str | None = None,
        on_created: Callable[[str], None] | None = None,
    ) -> str:
        """Wait for the user to decide on access to ``package``.

        Returns ``approved``, ``denied``, ``expired`` or ``error``. A standing
        grant the user gave earlier comes back as ``approved`` at once.
        """
        body: dict[str, Any] = {"kind": "app_access", "message": message, "context": context}
        if device_id:
            body["device_id"] = device_id
        if package:
            body["package"] = package
            body["app_name"] = app_name or package
        if screenshot:
            body["screenshot"] = screenshot
        async with self._client() as client:
            try:
                resp = await client.post("/internal/runtime/approvals", json=body)
            except httpx.HTTPError as exc:
                logger.error(f"[Managed] Approval request failed: {exc}")
                return "error"
            if resp.status_code != 201:
                logger.error(f"[Managed] Approval request rejected: HTTP {resp.status_code}")
                return "error"
            approval = resp.json()
            if approval.get("status") in FINAL_STATUSES:
                return str(approval["status"])
            approval_id = str(approval["id"])
            deadline = self._clock() + float(approval.get("expires_in", 0)) + GRACE_SECONDS
            if on_created is not None:
                on_created(approval_id)

            while approval.get("status") not in FINAL_STATUSES:
                if self._clock() > deadline:
                    return "expired"
                try:
                    resp = await client.get(
                        f"/internal/runtime/approvals/{approval_id}",
                        params={"wait": self._poll},
                        timeout=self._poll + 10.0,
                    )
                except httpx.HTTPError as exc:
                    logger.warning(f"[Managed] Approval poll failed, retrying: {exc}")
                    await self._sleep(self._retry_delay)
                    continue
                if resp.status_code == 200:
                    approval = resp.json()
                elif resp.status_code >= 500:
                    logger.warning(f"[Managed] Approval poll got HTTP {resp.status_code}, retrying")
                    await self._sleep(self._retry_delay)
                else:
                    logger.error(f"[Managed] Approval poll rejected: HTTP {resp.status_code}")
                    return "error"
            return str(approval["status"])
