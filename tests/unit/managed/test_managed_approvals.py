"""Managed mode: the control plane approval client."""

from __future__ import annotations

import json

import httpx
import pytest

from artemis.managed.approvals import ApprovalClient

PACKAGE = "com.example.bank"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(handler, clock: Clock | None = None) -> ApprovalClient:
    clock = clock or Clock()
    return ApprovalClient(
        "http://cp.test/",
        "runtime-token",
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )


async def _ask(client: ApprovalClient, **kwargs) -> str:
    return await client.request(
        "允许 agent 在「Bank」中操作？",
        context="artemis:s1",
        device_id="emulator-5554",
        package=PACKAGE,
        app_name="Bank",
        screenshot="/9j/AAAA",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_pending_then_approved():
    seen: list[httpx.Request] = []
    polls = iter(["pending", "approved"])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer runtime-token"
        if request.method == "POST":
            return httpx.Response(201, json={"id": "a1", "status": "pending", "expires_in": 600})
        assert request.url.path == "/internal/runtime/approvals/a1"
        assert request.url.params["wait"] == "25.0"
        return httpx.Response(200, json={"id": "a1", "status": next(polls), "expires_in": 1})

    created: list[str] = []
    assert await _ask(_client(handler), on_created=created.append) == "approved"
    assert created == ["a1"]
    body = json.loads(seen[0].content)
    assert body == {
        "kind": "app_access",
        "message": "允许 agent 在「Bank」中操作？",
        "context": "artemis:s1",
        "device_id": "emulator-5554",
        "package": PACKAGE,
        "app_name": "Bank",
        "screenshot": "/9j/AAAA",
    }
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_standing_grant_answers_at_once():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(201, json={"id": "", "status": "approved", "expires_in": 0})

    created: list[str] = []
    assert await _ask(_client(handler), on_created=created.append) == "approved"
    assert calls == ["POST"]
    assert created == []


@pytest.mark.asyncio
async def test_poll_errors_retry_then_rejections_fail_closed():
    clock = Clock()
    responses = iter(
        [
            httpx.Response(201, json={"id": "a1", "status": "pending", "expires_in": 600}),
            httpx.Response(502),
            httpx.Response(404),
        ]
    )
    assert await _ask(_client(lambda r: next(responses), clock)) == "error"
    assert clock.sleeps == [2.0]


@pytest.mark.parametrize(
    "response",
    [httpx.Response(401), httpx.Response(500), httpx.ConnectError("down")],
)
@pytest.mark.asyncio
async def test_create_failures_fail_closed(response):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    assert await _ask(_client(handler)) == "error"


@pytest.mark.asyncio
async def test_gives_up_after_the_deadline():
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"id": "a1", "status": "pending", "expires_in": 10})
        raise httpx.ReadTimeout("slow")

    assert await _ask(_client(handler, clock)) == "expired"
    assert clock.now > 10 + 30
