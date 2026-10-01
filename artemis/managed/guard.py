"""Ask the user before the agent acts inside guarded apps.

Artemis drives the phone itself, so the runtime's own app guard never sees
its actions. This wrapper applies the same rule at Artemis's single action
choke point (the actuator): before every input action it reads the
foreground package from the device -- never from the model -- and in a
guarded app (payment, banking) the user must allow the agent first. A grant
lasts for this run; the control plane may also answer at once from a grant
the user made permanent. A refusal stops the run.

It also refuses model-written arguments that the ADB driver would splice
into a device shell command unquoted (key names) or single-quoted (links).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable, Iterable
import io
import json
import os
import re
import socket
import time
from typing import Any

from PIL import Image

from artemis.config import read_ipc_port
from artemis.context import ArtemisContext
from artemis.drivers.factory import get_driver
from artemis.managed.approvals import ApprovalClient
from artemis.managed.settings import ManagedSettings, is_managed
from artemis.mcp.action_manifest import ExtensionTool
from artemis.mcp.action_types import ActionCode, ActionResult
from artemis.runtime.cancel_requests import request_cancel
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["GuardedActuator", "default_actuator", "is_guarded", "jpeg_thumbnail"]

#: Keys that only leave the current screen, so they never need approval.
_NAVIGATION_KEYS = frozenset({"back", "home"})
_KEY_NAME = re.compile(r"[A-Za-z0-9_]{1,64}")
#: Only a quote or a control character can escape the driver's single quotes.
_UNSAFE_LINK = re.compile(r"['\x00-\x1f\x7f]")
_FOREGROUND_RETRY_DELAY = 0.5
THUMBNAIL_WIDTH = 540


def is_guarded(package: str, guarded: Iterable[str]) -> bool:
    """Exact package, or a package under a guarded prefix (``com.icbc`` covers ``com.icbc.x``)."""
    return any(package == g or package.startswith(g + ".") for g in guarded)


def jpeg_thumbnail(screenshot_b64: str, width: int = THUMBNAIL_WIDTH) -> str | None:
    """A small JPEG of the screen for the approval page; ``None`` if unreadable."""
    try:
        with Image.open(io.BytesIO(base64.b64decode(screenshot_b64))) as img:
            img = img.convert("RGB")
            if img.width > width:
                img = img.resize((width, round(img.height * width / img.width)))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=60)
    except (OSError, ValueError, binascii.Error):
        return None
    return base64.b64encode(out.getvalue()).decode("ascii")


def publish_event(event_type: str, data: dict[str, Any]) -> None:
    """Best effort: forward an event to the server, which relays it on SSE."""
    port = read_ipc_port()
    if not port:
        return
    line = json.dumps({"event_type": event_type, "data": data}, default=str) + "\n"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as sock:
            sock.sendall(line.encode("utf-8"))
    except OSError:
        logger.debug(f"[Managed] Could not publish {event_type}")


def _session_id(ctx: ArtemisContext) -> str | None:
    session_id = os.environ.get("ARTEMIS_SESSION_ID")
    if session_id:
        return session_id
    engine = getattr(ctx, "data_engine", None)
    current = getattr(engine, "current_session_id", None)
    return str(current) if current else None


class GuardedActuator:
    """Wraps an actuator; see the module docstring."""

    def __init__(
        self,
        inner: Any,
        *,
        guarded: tuple[str, ...],
        app_names: dict[str, str] | None = None,
        approvals: ApprovalClient | None,
        publish: Callable[[str, dict[str, Any]], None] = publish_event,
        cancel: Callable[..., object] = request_cancel,
        retry_delay: float = _FOREGROUND_RETRY_DELAY,
    ) -> None:
        self._inner = inner
        self.ctx = inner.ctx
        self.controller = inner.controller
        self._guarded = guarded
        self._app_names = app_names or {}
        self._approvals = approvals
        self._publish = publish
        self._cancel = cancel
        self._retry_delay = retry_delay
        #: package -> whether the user allowed the agent in it during this run.
        self._decisions: dict[str, bool] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, inner: Any, settings: ManagedSettings) -> GuardedActuator:
        approvals = (
            ApprovalClient(settings.control_plane_url, settings.runtime_token)
            if settings.control_plane_url and settings.runtime_token
            else None
        )
        return cls(
            inner,
            guarded=settings.guarded_apps,
            app_names=settings.app_names,
            approvals=approvals,
        )

    # ------------------------------------------------------------ contract

    def capabilities(self) -> frozenset[str]:
        return self._inner.capabilities()

    def extensions(self) -> list[ExtensionTool]:
        # Backend tools would reach the device without passing the guard.
        return []

    # ------------------------------------------------------------ the guard

    async def _foreground_package(self) -> str | None:
        driver = get_driver(self.ctx)
        package = await driver.get_current_package()
        if not package:
            await asyncio.sleep(self._retry_delay)
            package = await driver.get_current_package()
        return package or None

    async def _screenshot(self) -> str | None:
        try:
            raw = await self._inner.take_screenshot()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # The screenshot only helps the user decide; ask without it.
            logger.warning(f"[Managed] No screenshot for the approval: {exc}")
            return None
        return jpeg_thumbnail(raw) if raw else None

    async def _refusal(self, action: str) -> ActionResult | None:
        """``None`` when ``action`` may run, else the result that replaces it."""
        if not self._guarded:
            return None
        package = await self._foreground_package()
        if package is None:
            # Cannot tell which app is in front: do not act blind.
            return ActionResult.failure(
                action,
                "Could not confirm which app is in the foreground; the action was not "
                "performed. Wait for the screen to settle and try again.",
            )
        if not is_guarded(package, self._guarded):
            return None
        async with self._lock:
            if package not in self._decisions:
                self._decisions[package] = await self._ask(package)
        if self._decisions[package]:
            return None
        name = self._app_names.get(package, package)
        return ActionResult.failure(
            action,
            f"The user did not allow the agent to act in {name}. The task has been "
            "stopped; report it as failed.",
            code=ActionCode.UNSUPPORTED,
        )

    async def _ask(self, package: str) -> bool:
        name = self._app_names.get(package, package)
        session_id = _session_id(self.ctx)
        if self._approvals is None:
            logger.error(f"[Managed] No control plane to ask about {package}; refusing")
            status = "error"
        else:
            device = getattr(self.ctx, "device", None)

            def on_created(approval_id: str) -> None:
                self._publish(
                    "approval_required",
                    {
                        "session_id": session_id,
                        "approval_id": approval_id,
                        "package": package,
                        "app_name": name,
                        "requested_at": time.time(),
                    },
                )

            try:
                status = await self._approvals.request(
                    f"允许 agent 在「{name}」中操作？",
                    context=f"artemis:{session_id or 'unknown'}",
                    device_id=getattr(device, "device_id", None),
                    package=package,
                    app_name=name if name != package else None,
                    screenshot=await self._screenshot(),
                    on_created=on_created,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                # e.g. a malformed reply: refuse like any other error.
                logger.error(
                    f"[Managed] Approval request for {package} crashed; refusing", exc_info=True
                )
                status = "error"
        allowed = status == "approved"
        self._publish(
            "approval_resolved",
            {"session_id": session_id, "package": package, "app_name": name, "status": status},
        )
        if allowed:
            logger.info(f"[Managed] Agent allowed in {package} for this run")
        else:
            logger.warning(f"[Managed] Agent not allowed in {package} ({status}); stopping")
            if session_id:
                self._cancel(session_id=session_id, reason=f"app access {status}")
        return allowed

    # ------------------------------------------------------- guarded inputs

    async def click_sequence(
        self, points: list[tuple[int, int]], delay_ms: int = 50
    ) -> ActionResult:
        return await self._refusal("click_sequence") or await self._inner.click_sequence(
            points, delay_ms
        )

    async def click(self, nx: int, ny: int, times: int = 1, delay_ms: int = 100) -> ActionResult:
        return await self._refusal("click") or await self._inner.click(nx, ny, times, delay_ms)

    async def long_press(self, nx: int, ny: int, duration_ms: int = 1000) -> ActionResult:
        return await self._refusal("long_press") or await self._inner.long_press(
            nx, ny, duration_ms
        )

    async def input_text(
        self,
        text: str,
        target: tuple[int, int] | None = None,
        clear_exist: bool = True,
    ) -> ActionResult:
        return await self._refusal("input_text") or await self._inner.input_text(
            text, target, clear_exist
        )

    async def swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int = 800,
    ) -> ActionResult:
        return await self._refusal("swipe") or await self._inner.swipe(start, end, duration_ms)

    async def press_key(self, key: str) -> ActionResult:
        key_name = str(key)
        if not _KEY_NAME.fullmatch(key_name):
            return ActionResult.failure(
                "press_key", f"Invalid key name '{key_name}'.", code=ActionCode.INVALID_ARGS
            )
        if key_name.lower() in _NAVIGATION_KEYS:
            return await self._inner.press_key(key_name)
        return await self._refusal("press_key") or await self._inner.press_key(key_name)

    async def erase_one_char(self) -> ActionResult:
        return await self._refusal("erase_one_char") or await self._inner.erase_one_char()

    async def focus_and_clear_text(self, nx: int, ny: int) -> ActionResult:
        return await self._refusal(
            "focus_and_clear_text"
        ) or await self._inner.focus_and_clear_text(nx, ny)

    # ------------------------------------------- launching, waiting, reading

    async def manage_app(self, action: str, app_name: str) -> ActionResult:
        # Resolved against installed packages; opening a guarded app is fine,
        # acting in it is not.
        return await self._inner.manage_app(action, app_name)

    async def open_link(self, url: str) -> ActionResult:
        if _UNSAFE_LINK.search(url):
            return ActionResult.failure(
                "open_link",
                "Links may not contain quotes or control characters.",
                code=ActionCode.INVALID_ARGS,
            )
        return await self._inner.open_link(url)

    async def wait_for_delay(self, time_in_ms: int) -> ActionResult:
        return await self._inner.wait_for_delay(time_in_ms)

    async def wait_for_text(
        self, text: str, wait_state: str | None = None, timeout_ms: int | None = None
    ) -> ActionResult:
        return await self._inner.wait_for_text(text, wait_state, timeout_ms)

    async def take_screenshot(self) -> str:
        return await self._inner.take_screenshot()

    async def get_ui_elements(self) -> Any:
        return await self._inner.get_ui_elements()

    async def get_screen_data(self) -> Any:
        return await self._inner.get_screen_data()


def default_actuator(ctx: ArtemisContext, controller: Any = None) -> Any:
    """The ADB actuator, behind the app guard in managed deployments.

    The guarded actuator is stored on the context, so every agent of a run
    shares one set of the user's decisions.
    """
    from artemis.mcp.actuators.adb import AdbActuator

    actuator = AdbActuator(ctx, controller)
    if not is_managed():
        return actuator
    guarded = GuardedActuator.from_settings(actuator, ManagedSettings.from_env())
    ctx.actuator = guarded
    return guarded
