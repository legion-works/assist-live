import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tools import fault_runner, ha_websocket, test_environment

TOKEN = "example-test-credential"
SITES = ("fault_runner", "test_environment", "record_device")


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "authentication.json"
    path.write_text(json.dumps({"token": TOKEN}))
    return path


async def call_site(site, url, token_file, output, monkeypatch):
    monkeypatch.setattr(ha_websocket, "HA_PORT", url.port)
    origin = str(url.origin())
    if site == "fault_runner":
        monkeypatch.setattr(fault_runner, "URL", origin)
        async with ha_websocket.ha_session() as session:
            async with await fault_runner.connect(session, token_file) as ws:
                assert not ws.closed
                assert (await ws.receive()).type is aiohttp.WSMsgType.CLOSE
        return []
    monkeypatch.setattr(test_environment, "URL", origin)
    if site == "test_environment":
        return await test_environment.run_live({}, token_file, None, remove=True)
    return await test_environment.record_device(token_file, output, 1, "run", "config")


@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.parametrize("site", SITES)
async def test_ha_websocket_refuses_redirect(site, token_file, tmp_path, monkeypatch):
    requests = []

    async def unexpected(request):
        requests.append(request.path)
        return web.Response(status=403)

    target = web.Application()
    target.router.add_get("/target", unexpected)
    async with TestServer(target, host="127.0.0.1", port=0) as destination:
        location = destination.make_url("/target?example-query=example-value")

        async def redirect(_request):
            raise web.HTTPTemporaryRedirect(location=location)

        app = web.Application()
        app.router.add_get("/api/websocket", redirect)
        async with TestServer(app, host="127.0.0.1", port=0) as origin:
            async with asyncio.timeout(5):
                with pytest.raises(ValueError, match="redirect refused") as refusal:
                    await call_site(
                        site,
                        origin.make_url("/"),
                        token_file,
                        tmp_path / "out",
                        monkeypatch,
                    )
    assert requests == []
    assert TOKEN not in str(refusal.value)
    assert "example-query" not in str(refusal.value)
    assert "example-value" not in str(refusal.value)


@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.parametrize("site", SITES)
async def test_ha_websocket_authenticates_directly(
    site, token_file, tmp_path, monkeypatch
):
    messages = []

    async def authenticate(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.10.3"})
        messages.append(await ws.receive_json())
        await ws.send_json({"type": "auth_ok"})
        if site == "fault_runner":
            await ws.close()
            return ws
        if site == "record_device":
            messages.append(await ws.receive_json())
            await ws.send_json({"id": 1, "success": True})
            await ws.send_json(
                {"type": "event", "event": {"data": {"turn_id": "test"}}}
            )
        async for _ in ws:
            pass
        return ws

    async def entries(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return web.json_response([])

    monkeypatch.setattr(test_environment, "validate_run_evidence", lambda *_, **__: [])
    app = web.Application()
    app.router.add_get("/api/websocket", authenticate)
    app.router.add_get("/api/config/config_entries/entry", entries)
    output = tmp_path / "out"
    async with TestServer(app, host="127.0.0.1", port=0) as origin:
        async with asyncio.timeout(5):
            result = await call_site(
                site, origin.make_url("/"), token_file, output, monkeypatch
            )
            assert result == []

    expected = [{"type": "auth", "access_token": TOKEN}]
    if site == "record_device":
        expected.append(
            {
                "id": 1,
                "type": "subscribe_events",
                "event_type": "assist_live_test_record",
            }
        )
        assert json.loads(output.read_text())["turn_id"] == "test"
    assert messages == expected


@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.parametrize("status", [307, 308])
async def test_ha_session_refuses_http_redirect_with_token(status):
    requests = []

    async def unexpected(request):
        requests.append(request.headers.get("Authorization"))
        return web.json_response([])

    target = web.Application()
    target.router.add_get("/target", unexpected)
    async with TestServer(target, host="127.0.0.1", port=0) as destination:
        location = destination.make_url("/target")

        async def moved(_request):
            return web.Response(status=status, headers={"Location": str(location)})

        app = web.Application()
        app.router.add_get("/api/config/config_entries/entry", moved)
        async with (
            TestServer(app, host="127.0.0.1", port=0) as origin,
            ha_websocket.ha_session(
                headers={"Authorization": f"Bearer {TOKEN}"}
            ) as session,
        ):
            async with asyncio.timeout(5):
                with pytest.raises(ValueError, match="redirect refused"):
                    await session.get(
                        origin.make_url("/api/config/config_entries/entry")
                    )
    assert requests == []


async def test_fault_runner_refuses_session_without_redirect_guard(
    token_file, monkeypatch
):
    monkeypatch.setattr(fault_runner, "URL", "not-a-url")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ValueError, match="redirect refusal"):
            await fault_runner.connect(session, token_file)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:18123",
        "http://localhost:18123",
        "http://127.0.0.1:18124",
        "http://127.0.0.1:18123/path",
        "http://127.0.0.1:18123?value=1",
        "http://127.0.0.1:18123#fragment",
        "http://user@127.0.0.1:18123",
    ],
)
def test_rejects_other_ha_urls(url):
    with pytest.raises(ValueError, match="isolated loopback test instance"):
        ha_websocket.checked_ha_url(url)
