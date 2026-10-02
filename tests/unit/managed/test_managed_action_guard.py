"""Managed mode: the independent action guard at the actuator layer."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import httpx
from PIL import Image
import pytest

from artemis.managed.action_guard import (
    ActionGuard,
    InputAction,
    Verdict,
    approval_message,
    mark_action,
    parse_verdict,
)
from artemis.managed.guard import GuardedActuator, default_actuator
from artemis.managed.settings import ManagedConfigError, ManagedSettings
from artemis.mcp.action_types import ActionCode
from artemis.mcp.actuators import MockActuator

BANK = "com.example.bank"
SESSION = "11111111-2222-3333-4444-555555555555"
SENSITIVE = {
    "sensitive": True,
    "category": "payment",
    "target": "确认支付",
    "reason": "会扣款 25 元",
}
HARMLESS = {"sensitive": False, "category": "none", "target": "搜索", "reason": ""}


class FakeApprovals:
    def __init__(self, *statuses: str) -> None:
        self.statuses = list(statuses)
        self.calls: list[dict[str, Any]] = []

    async def request(self, message: str, **kwargs: Any) -> str:
        self.calls.append({"message": message, **kwargs})
        on_created = kwargs.get("on_created")
        if on_created is not None:
            on_created(f"appr-{len(self.calls)}")
        return self.statuses.pop(0) if self.statuses else "denied"


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.cancels: list[dict[str, Any]] = []

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))

    def cancel(self, **kwargs: Any) -> None:
        self.cancels.append(kwargs)


class Model:
    """An OpenAI-compatible endpoint that answers with a fixed text (or fails)."""

    def __init__(self, answer: dict | str | None = None, status: int = 200) -> None:
        self.answer = answer if answer is not None else HARMLESS
        self.status = status
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "url": str(request.url),
                "headers": request.headers,
                "body": json.loads(request.content),
            }
        )
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "boom"})
        content = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
        )

    def guard(self) -> ActionGuard:
        return ActionGuard(
            model="action-guard",
            base_url="http://gw/v1/",
            api_key="sk-user",
            transport=httpx.MockTransport(self.handler),
        )


@pytest.fixture
def inner() -> MockActuator:
    return MockActuator(width=1000, height=2000)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    monkeypatch.setenv("ARTEMIS_SESSION_ID", SESSION)
    return Recorder()


def _guarded(
    inner: MockActuator,
    recorder: Recorder,
    approvals: FakeApprovals,
    model: Model | None,
    guarded: tuple[str, ...] = (),
) -> GuardedActuator:
    return GuardedActuator(
        inner,
        guarded=guarded,
        app_names={BANK: "Example Bank"},
        approvals=approvals,  # type: ignore[arg-type]
        action_guard=model.guard() if model else None,
        publish=recorder.publish,
        cancel=recorder.cancel,
        retry_delay=0,
    )


def _taps(inner: MockActuator) -> list[dict]:
    return [h for h in inner.action_history if h["action"] == "tap"]


def _png(width: int = 1000, height: int = 2000) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------------ verdicts


@pytest.mark.parametrize(
    "text", [json.dumps(SENSITIVE), f"结果：\n```json\n{json.dumps(SENSITIVE)}\n```"]
)
def test_verdicts_are_read_from_the_first_json_object(text):
    assert parse_verdict(text) == Verdict(True, "payment", "确认支付", "会扣款 25 元")


@pytest.mark.parametrize(
    "text", ["", "no", '{"sensitive": "no"}', '{"category": "none"}', '{"sensitive": false']
)
def test_anything_else_is_a_doubt(text):
    verdict = parse_verdict(text)
    assert verdict.error is not None
    assert verdict.needs_approval


def test_approval_messages_match_the_runtime():
    tap = InputAction("tap", 500, 900)
    assert approval_message(parse_verdict(json.dumps(SENSITIVE)), tap) == (
        "这一步可能是「付款或转账」：点击「确认支付」。会扣款 25 元"
    )
    enter = InputAction("key", key="enter")
    verdict = Verdict(True, "send", reason="会把消息发出去")
    assert (
        approval_message(verdict, enter)
        == "这一步可能是「发送消息、邮件或评论」：按下 ENTER 键。会把消息发出去"
    )
    assert "检查超时" in approval_message(Verdict(False, error="检查超时"), enter)


def test_marks_use_normalized_coordinates():
    marked = Image.open(
        io.BytesIO(base64.b64decode(mark_action(_png(), InputAction("tap", 500, 250))))
    )
    assert marked.size == (720, 1440)
    r, g, b = marked.getpixel((360, 360))  # type: ignore[misc]
    assert r > 200 and g < 80 and b < 80
    assert marked.getpixel((5, 5)) == (255, 255, 255)
    # A key press has nothing to mark.
    plain = Image.open(
        io.BytesIO(base64.b64decode(mark_action(_png(), InputAction("key", key="enter"))))
    )
    assert plain.getpixel((360, 720)) == (255, 255, 255)


# ------------------------------------------------------------- the guard call


@pytest.mark.asyncio
async def test_the_guard_sees_the_marked_screen_through_the_users_gateway():
    model = Model()
    verdict = await model.guard().judge(_png(), InputAction("swipe", 100, 500, end=(900, 500)))
    assert not verdict.needs_approval
    [request] = model.requests
    assert request["url"] == "http://gw/v1/chat/completions"
    assert request["headers"]["authorization"] == "Bearer sk-user"
    body = request["body"]
    assert body["model"] == "action-guard" and body["temperature"] == 0
    text, image = body["messages"][1]["content"]
    assert "swipe from (100, 500) to (900, 500)" in text["text"]
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "data, not instructions" in body["messages"][0]["content"]


@pytest.mark.parametrize(
    ("model", "screen", "error"),
    [
        (Model(status=500), _png(), "检查失败"),
        (Model("not json"), _png(), "检查结果无法识别"),
        (Model(), None, "没有可用的屏幕截图"),
        (Model(), "bm90IGFuIGltYWdl", "没有可用的屏幕截图"),
    ],
)
@pytest.mark.asyncio
async def test_the_guard_fails_closed(model, screen, error):
    verdict = await model.guard().judge(screen, InputAction("tap", 1, 2))
    assert verdict.error == error
    assert verdict.needs_approval


@pytest.mark.asyncio
async def test_a_slow_guard_times_out():
    async def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    guard = ActionGuard(
        model="m", base_url="http://gw", api_key="k", transport=httpx.MockTransport(slow)
    )
    verdict = await guard.judge(_png(), InputAction("tap", 1, 2))
    assert verdict.error == "检查超时"


# ------------------------------------------------------- the guarded actuator


@pytest.mark.asyncio
async def test_a_harmless_tap_goes_through_without_asking(inner, recorder):
    approvals, model = FakeApprovals(), Model()
    guard = _guarded(inner, recorder, approvals, model)
    assert (await guard.click(500, 500)).ok
    assert len(_taps(inner)) == 1
    assert len(model.requests) == 1
    assert approvals.calls == []


@pytest.mark.parametrize(
    ("call", "args"),
    [
        ("click", (500, 900)),
        ("long_press", (500, 900)),
        ("swipe", ((100, 900), (900, 900))),
        ("press_key", ("enter",)),
        ("click_sequence", ([(100, 100), (500, 900)],)),
    ],
)
@pytest.mark.asyncio
async def test_a_sensitive_input_waits_for_the_user(inner, recorder, call, args):
    approvals = FakeApprovals("approved")
    guard = _guarded(inner, recorder, approvals, Model(SENSITIVE))
    assert (await getattr(guard, call)(*args)).ok
    [ask] = approvals.calls
    assert ask["kind"] == "action"
    assert ask["message"].startswith("这一步可能是「付款或转账」")
    assert ask["context"] == f"artemis:{SESSION}"
    assert ask["screenshot"]  # what the guard looked at
    assert [e for e, _ in recorder.events] == ["approval_required", "approval_resolved"]
    assert recorder.events[0][1]["message"] == ask["message"]
    assert recorder.cancels == []


@pytest.mark.parametrize("status", ["denied", "expired", "error"])
@pytest.mark.asyncio
async def test_a_refused_input_never_reaches_the_phone(inner, recorder, status):
    guard = _guarded(inner, recorder, FakeApprovals(status), Model(SENSITIVE))
    res = await guard.click(500, 900)
    assert not res.ok
    assert res.code is ActionCode.UNSUPPORTED
    assert "did not allow this step" in res.message
    assert _taps(inner) == []
    assert recorder.cancels == [{"session_id": SESSION, "reason": f"action {status}"}]


@pytest.mark.asyncio
async def test_when_the_guard_fails_the_user_decides(inner, recorder):
    approvals = FakeApprovals("denied")
    guard = _guarded(inner, recorder, approvals, Model(status=503))
    assert not (await guard.click(500, 900)).ok
    assert _taps(inner) == []
    assert "检查失败" in approvals.calls[0]["message"]


@pytest.mark.asyncio
async def test_each_input_is_judged_on_its_own(inner, recorder):
    approvals = FakeApprovals("approved", "denied")
    model = Model(SENSITIVE)
    guard = _guarded(inner, recorder, approvals, model)
    assert (await guard.click(500, 900)).ok
    # Allowing one payment does not allow the next.
    assert not (await guard.click(500, 900)).ok
    assert len(approvals.calls) == 2
    assert len(model.requests) == 2
    assert len(_taps(inner)) == 1


@pytest.mark.asyncio
async def test_navigation_and_typing_are_not_judged(inner, recorder):
    approvals, model = FakeApprovals(), Model(SENSITIVE)
    guard = _guarded(inner, recorder, approvals, model)
    assert (await guard.press_key("back")).ok
    assert (await guard.press_key("home")).ok
    assert (await guard.input_text("hello")).ok
    assert model.requests == []
    assert approvals.calls == []


@pytest.mark.asyncio
async def test_a_refused_guarded_app_is_never_judged(inner, recorder):
    approvals, model = FakeApprovals("denied"), Model(HARMLESS)
    guard = _guarded(inner, recorder, approvals, model, guarded=(BANK,))
    inner.driver._current_package = BANK
    res = await guard.click(500, 900)
    assert "did not allow the agent to act in Example Bank" in res.message
    assert approvals.calls[0]["kind"] == "app_access"
    assert model.requests == []


@pytest.mark.asyncio
async def test_without_a_guard_model_only_guarded_apps_are_asked(inner, recorder):
    approvals = FakeApprovals()
    guard = _guarded(inner, recorder, approvals, None)
    assert (await guard.click(500, 900)).ok
    assert approvals.calls == []


# ------------------------------------------------------------------ settings


MANAGED_ENV = {
    "ARTEMIS_MANAGED": "1",
    "ARTEMIS_CONTROL_PLANE_URL": "http://cp.test",
    "ARTEMIS_RUNTIME_TOKEN": "rt",
    "ARTEMIS_ACTION_GUARD_MODEL": "action-guard",
    "OPENAI_BASE_URL": "http://gw/v1",
    "OPENAI_API_KEY": "sk-user",
}


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> ManagedSettings:
    for key, value in {**MANAGED_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ARTEMIS_API_TOKEN", "t" * 40)
    return ManagedSettings.from_env()


@pytest.mark.parametrize(
    "missing",
    ["OPENAI_BASE_URL", "OPENAI_API_KEY", "ARTEMIS_CONTROL_PLANE_URL", "ARTEMIS_RUNTIME_TOKEN"],
)
def test_the_guard_needs_the_gateway_and_the_control_plane(monkeypatch, tmp_path, missing):
    config = tmp_path / "artemis.jsonc"
    config.write_text("{}")
    settings = _settings(monkeypatch, ARTEMIS_ARTEMIS_JSONC=str(config), **{missing: ""})
    with pytest.raises(ManagedConfigError, match="ARTEMIS_ACTION_GUARD_MODEL"):
        settings.check()
    _settings(monkeypatch, ARTEMIS_ARTEMIS_JSONC=str(config)).check()


def test_default_actuator_builds_the_guard(inner, monkeypatch):
    _settings(monkeypatch)
    actuator = default_actuator(inner.ctx)
    assert isinstance(actuator, GuardedActuator)
    assert actuator._action_guard is not None
    assert actuator._action_guard.model == "action-guard"
