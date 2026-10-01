"""Managed mode: the admin server's API surface, settings and model routing."""

from __future__ import annotations

import os

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from artemis.managed.server import ManagedApiMiddleware, check_run_request, install
from artemis.managed.settings import ManagedConfigError, ManagedSettings, parse_guarded_apps

TOKEN = "t" * 40
SID = "0b8a3f4e-5d6c-4b7a-8e9f-0a1b2c3d4e5f"


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/api/run")
    async def run() -> dict:
        return {"ok": True}

    @app.get("/api/stream/{sid}")
    async def stream(sid: str) -> dict:
        return {"sid": sid}

    @app.get("/api/system/credentials")
    async def credentials() -> dict:
        return {"secret": "x"}

    @app.get("/{path:path}")
    async def spa(path: str) -> dict:
        return {"spa": path}

    app.add_middleware(ManagedApiMiddleware, token=TOKEN)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8100")


@pytest.mark.parametrize("auth", [None, "Bearer wrong", f"Basic {TOKEN}", f"Bearer {TOKEN}x"])
@pytest.mark.asyncio
async def test_requests_without_the_token_are_rejected(auth):
    headers = {"Authorization": auth} if auth else {}
    async with _client(_app()) as client:
        res = await client.post("/api/run", headers=headers)
    assert res.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("POST", "/api/run", 200),
        ("GET", f"/api/stream/{SID}", 200),
        ("GET", "/api/stream/device-live", 404),
        ("GET", "/api/system/credentials", 404),
        ("POST", "/api/system/credentials", 404),
        ("GET", "/api/run", 404),
        ("GET", "/admin", 404),
        ("GET", "/docs", 404),
    ],
)
@pytest.mark.asyncio
async def test_only_runtime_routes_are_served(method, path, status):
    async with _client(_app()) as client:
        res = await client.request(method, path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert res.status_code == status


@pytest.mark.parametrize(
    ("fields", "refusal"),
    [
        ({"goal": "g", "session_id": SID}, None),
        ({"goal": "g", "session_id": SID, "profile": "Flash", "device_serial": "x"}, None),
        ({"goal": "g", "session_id": SID, "profile": "pro"}, "Only the 'flash' profile"),
        ({"goal": "g", "session_id": SID, "app_path": "/x.apk"}, "app_path"),
        ({"goal": "g", "session_id": SID, "goals": ["a", "b"]}, "goals"),
        ({"goal": "g", "session_id": SID, "explorer_mode": "ultra"}, "explorer_mode"),
        ({"goal": "g"}, "UUID session_id"),
        ({"goal": "g", "session_id": "../x"}, "UUID session_id"),
        ({"session_id": SID}, "goal"),
    ],
)
def test_run_requests(fields, refusal):
    result = check_run_request(fields)
    if refusal is None:
        assert result is None
    else:
        assert result is not None and refusal in result


@pytest.mark.asyncio
async def test_admin_run_route_refuses_pro_in_managed_mode(monkeypatch):
    from apps.admin_console.routers import tasks
    from apps.admin_console.server import app

    monkeypatch.setattr(tasks, "is_managed", lambda: True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        res = await c.post("/api/run", json={"goal": "g", "profile": "pro", "session_id": SID})
    assert res.status_code == 403
    assert "flash" in res.json()["detail"]


def test_parse_guarded_apps():
    assert parse_guarded_apps(" com.a=App A, com.b ,, =x") == (
        ("com.a", "com.b"),
        {"com.a": "App A"},
    )
    assert parse_guarded_apps(None) == ((), {})


def _settings(**overrides) -> ManagedSettings:
    values = {
        "api_token": TOKEN,
        "guarded_apps": ("com.a",),
        "app_names": {},
        "control_plane_url": "http://cp",
        "runtime_token": "rt",
        "utility_model": "artemis-flash-lite",
        "config_path": __file__,
    }
    values.update(overrides)
    return ManagedSettings(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"api_token": "short"}, "ARTEMIS_API_TOKEN"),
        ({"config_path": ""}, "ARTEMIS_ARTEMIS_JSONC"),
        ({"config_path": "/nope.jsonc"}, "ARTEMIS_ARTEMIS_JSONC"),
        ({"control_plane_url": ""}, "ARTEMIS_CONTROL_PLANE_URL"),
        ({"runtime_token": ""}, "ARTEMIS_RUNTIME_TOKEN"),
    ],
)
def test_unsafe_settings_refuse_to_start(overrides, message):
    with pytest.raises(ManagedConfigError, match=message):
        _settings(**overrides).check()


def test_settings_without_guarded_apps_need_no_control_plane():
    _settings(guarded_apps=(), control_plane_url="", runtime_token="").check()


def test_install_locks_the_app_and_keeps_phones_free_to_sleep(monkeypatch):
    monkeypatch.setenv("ARTEMIS_API_TOKEN", TOKEN)
    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", __file__)
    monkeypatch.delenv("ARTEMIS_GUARDED_APPS", raising=False)
    monkeypatch.setenv("ARTEMIS_KEEP_DEVICE_AWAKE", "true")
    app = FastAPI()
    install(app)
    assert [m.cls for m in app.user_middleware] == [ManagedApiMiddleware]
    assert os.environ["ARTEMIS_KEEP_DEVICE_AWAKE"] == "false"

    monkeypatch.delenv("ARTEMIS_API_TOKEN")
    with pytest.raises(ManagedConfigError):
        install(FastAPI())


def test_gemini_helpers_use_the_gateway_in_managed_mode(monkeypatch):
    from langchain_openai import ChatOpenAI

    from artemis.services.llm import get_google_llm

    monkeypatch.delenv("ARTEMIS_FAKE_LLM", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "gateway-key")
    monkeypatch.setenv("ARTEMIS_MANAGED", "1")
    monkeypatch.setenv("ARTEMIS_MANAGED_UTILITY_MODEL", "artemis-flash-lite")
    llm = get_google_llm(model_name="gemini-2.5-flash-lite", temperature=0.0)
    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == "artemis-flash-lite"
