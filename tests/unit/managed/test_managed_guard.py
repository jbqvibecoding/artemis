"""Managed mode: the app guard at the actuator layer."""

from __future__ import annotations

import base64
import io
from typing import Any

from PIL import Image
import pytest

from artemis.managed.guard import GuardedActuator, default_actuator, is_guarded, jpeg_thumbnail
from artemis.mcp.action_types import ActionCode
from artemis.mcp.actuators import Actuator, AdbActuator, MockActuator

BANK = "com.example.bank"


class FakeApprovals:
    def __init__(self, status: str = "approved") -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    async def request(self, message: str, **kwargs: Any) -> str:
        self.calls.append({"message": message, **kwargs})
        on_created = kwargs.get("on_created")
        if on_created is not None:
            on_created("appr-1")
        return self.status


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.cancels: list[dict[str, Any]] = []

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))

    def cancel(self, **kwargs: Any) -> None:
        self.cancels.append(kwargs)


@pytest.fixture
def inner() -> MockActuator:
    return MockActuator(width=1000, height=2000)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    monkeypatch.setenv("ARTEMIS_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    return Recorder()


def _guard(
    inner: MockActuator,
    recorder: Recorder,
    approvals: FakeApprovals | None,
    guarded: tuple[str, ...] = (BANK,),
) -> GuardedActuator:
    return GuardedActuator(
        inner,
        guarded=guarded,
        app_names={BANK: "Example Bank"},
        approvals=approvals,  # type: ignore[arg-type]
        publish=recorder.publish,
        cancel=recorder.cancel,
        retry_delay=0,
    )


def _in_front(inner: MockActuator, package: str | None) -> None:
    inner.driver._current_package = package


def _taps(inner: MockActuator) -> list[dict]:
    return [h for h in inner.action_history if h["action"] == "tap"]


def test_satisfies_the_actuator_contract(inner, recorder):
    guard = _guard(inner, recorder, FakeApprovals())
    assert isinstance(guard, Actuator)
    assert guard.capabilities() == inner.capabilities()
    assert guard.extensions() == []
    assert guard.controller is inner.controller


@pytest.mark.parametrize(
    ("package", "guarded"),
    [(BANK, True), (BANK + ".wallet", True), (BANK + "x", False), ("com.example", False)],
)
def test_guarded_packages(package, guarded):
    assert is_guarded(package, (BANK,)) is guarded


@pytest.mark.asyncio
async def test_unguarded_app_is_not_asked(inner, recorder):
    approvals = FakeApprovals()
    guard = _guard(inner, recorder, approvals)
    res = await guard.click(500, 500)
    assert res.ok
    assert len(_taps(inner)) == 1
    assert approvals.calls == []


@pytest.mark.asyncio
async def test_guarded_app_asks_once_per_run(inner, recorder):
    approvals = FakeApprovals("approved")
    guard = _guard(inner, recorder, approvals)
    _in_front(inner, BANK)

    assert (await guard.click(500, 500)).ok
    assert (await guard.input_text("hello")).ok
    assert (await guard.swipe((500, 800), (500, 200), 100)).ok

    [call] = approvals.calls
    assert call["message"] == "允许 agent 在「Example Bank」中操作？"
    assert call["package"] == BANK
    assert call["app_name"] == "Example Bank"
    assert call["context"] == "artemis:11111111-2222-3333-4444-555555555555"
    assert call["screenshot"]  # a JPEG thumbnail of the screen
    assert [e for e, _ in recorder.events] == ["approval_required", "approval_resolved"]
    assert recorder.events[0][1]["approval_id"] == "appr-1"
    assert recorder.events[1][1]["status"] == "approved"
    assert recorder.cancels == []


@pytest.mark.parametrize("status", ["denied", "expired", "error"])
@pytest.mark.asyncio
async def test_refusal_blocks_the_action_and_stops_the_run(inner, recorder, status):
    approvals = FakeApprovals(status)
    guard = _guard(inner, recorder, approvals)
    _in_front(inner, BANK)

    res = await guard.click(500, 500)
    assert not res.ok
    assert res.code is ActionCode.UNSUPPORTED
    assert "did not allow the agent to act in Example Bank" in res.message
    assert _taps(inner) == []
    assert recorder.cancels == [
        {"session_id": "11111111-2222-3333-4444-555555555555", "reason": f"app access {status}"}
    ]
    assert recorder.events[-1][1]["status"] == status

    # The refusal is remembered: no second question, still nothing done.
    assert not (await guard.long_press(500, 500)).ok
    assert len(approvals.calls) == 1
    assert inner.action_history == []


@pytest.mark.asyncio
async def test_no_control_plane_refuses(inner, recorder):
    guard = _guard(inner, recorder, approvals=None)
    _in_front(inner, BANK)
    assert not (await guard.click(500, 500)).ok
    assert _taps(inner) == []


@pytest.mark.asyncio
async def test_unknown_foreground_app_refuses_the_action_only(inner, recorder):
    approvals = FakeApprovals()
    guard = _guard(inner, recorder, approvals)
    _in_front(inner, None)

    res = await guard.click(500, 500)
    assert not res.ok
    assert "Could not confirm which app is in the foreground" in res.message
    assert _taps(inner) == []
    assert approvals.calls == []
    assert recorder.cancels == []  # a later action may still run


@pytest.mark.asyncio
async def test_no_guarded_apps_skips_the_check(inner, recorder):
    guard = _guard(inner, recorder, FakeApprovals(), guarded=())
    _in_front(inner, None)
    assert (await guard.click(500, 500)).ok


@pytest.mark.asyncio
async def test_navigation_keys_need_no_approval(inner, recorder):
    approvals = FakeApprovals("denied")
    guard = _guard(inner, recorder, approvals)
    _in_front(inner, BANK)

    assert (await guard.press_key("back")).ok
    assert (await guard.press_key("HOME")).ok
    assert approvals.calls == []
    assert not (await guard.press_key("enter")).ok
    assert len(approvals.calls) == 1


@pytest.mark.parametrize("key", ["3; rm -rf /sdcard", "66 && reboot", "", "a" * 65])
@pytest.mark.asyncio
async def test_key_names_cannot_reach_the_shell(inner, recorder, key):
    guard = _guard(inner, recorder, FakeApprovals(), guarded=())
    res = await guard.press_key(key)
    assert res.code is ActionCode.INVALID_ARGS
    assert inner.action_history == []


@pytest.mark.parametrize("url", ["https://x.test/'; reboot; '", "https://x.test/\nreboot"])
@pytest.mark.asyncio
async def test_links_cannot_break_out_of_quotes(inner, recorder, url):
    guard = _guard(inner, recorder, FakeApprovals(), guarded=())
    res = await guard.open_link(url)
    assert res.code is ActionCode.INVALID_ARGS
    assert inner.action_history == []


@pytest.mark.asyncio
async def test_waiting_and_reading_need_no_approval(inner, recorder):
    approvals = FakeApprovals("denied")
    guard = _guard(inner, recorder, approvals)
    _in_front(inner, BANK)
    assert (await guard.wait_for_delay(1)).ok
    assert await guard.take_screenshot()
    assert approvals.calls == []


def test_jpeg_thumbnail():
    buf = io.BytesIO()
    Image.new("RGB", (1080, 2400), "white").save(buf, format="PNG")
    thumb = jpeg_thumbnail(base64.b64encode(buf.getvalue()).decode())
    assert thumb is not None
    with Image.open(io.BytesIO(base64.b64decode(thumb))) as img:
        assert (img.format, img.width, img.height) == ("JPEG", 540, 1200)
    assert jpeg_thumbnail("not an image") is None


def test_default_actuator_is_plain_adb_outside_managed_mode(inner, monkeypatch):
    monkeypatch.delenv("ARTEMIS_MANAGED", raising=False)
    actuator = default_actuator(inner.ctx)
    assert type(actuator) is AdbActuator
    assert inner.ctx.actuator is None


def test_default_actuator_is_guarded_and_shared_in_managed_mode(inner, monkeypatch):
    monkeypatch.setenv("ARTEMIS_MANAGED", "1")
    monkeypatch.setenv("ARTEMIS_GUARDED_APPS", f"{BANK}=Example Bank, com.other")
    monkeypatch.setenv("ARTEMIS_CONTROL_PLANE_URL", "http://cp.test")
    monkeypatch.setenv("ARTEMIS_RUNTIME_TOKEN", "rt")
    actuator = default_actuator(inner.ctx)
    assert isinstance(actuator, GuardedActuator)
    assert inner.ctx.actuator is actuator
    assert actuator._guarded == (BANK, "com.other")
    assert actuator._app_names == {BANK: "Example Bank"}
    assert actuator._approvals is not None


class CrashingApprovals:
    async def request(self, message: str, **kwargs: Any) -> str:
        raise KeyError("id")


@pytest.mark.asyncio
async def test_a_crashing_approval_request_refuses(inner, recorder):
    guard = _guard(inner, recorder, CrashingApprovals())  # type: ignore[arg-type]
    _in_front(inner, BANK)
    assert not (await guard.click(500, 500)).ok
    assert _taps(inner) == []
    assert recorder.cancels


@pytest.mark.asyncio
async def test_a_failing_screenshot_still_asks(inner, recorder, monkeypatch):
    approvals = FakeApprovals("approved")
    guard = _guard(inner, recorder, approvals)
    _in_front(inner, BANK)

    async def broken() -> str:
        raise ConnectionError("adb gone")

    monkeypatch.setattr(inner, "take_screenshot", broken)
    assert (await guard.click(500, 500)).ok
    assert approvals.calls[0]["screenshot"] is None
