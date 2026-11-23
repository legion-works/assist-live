import asyncio
from functools import partial

import pytest
from aiohttp import WSMessage, web
from aiohttp.test_utils import TestServer

from tools.probe_pipelines import connect_authenticated

TOKEN = "example-test-credential"
QUERY_NAME = "test-query"
QUERY_VALUE = "example-value"
QUERY = f"{QUERY_NAME}={QUERY_VALUE}"


async def accept_websocket(
    request: web.Request, *, requests: list[str], messages: list[WSMessage]
) -> web.WebSocketResponse:
    requests.append(request.path)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_json({"type": "auth_required", "ha_version": "2026.10.3"})
    async for message in ws:
        messages.append(message)
        await ws.send_json({"type": "auth_ok"})
    return ws


async def assert_redirect_refused(
    url: str, requests: list[str], messages: list[WSMessage]
) -> None:
    refusal = None
    try:
        async with asyncio.timeout(5), connect_authenticated(url, TOKEN):
            pass
    except ValueError as error:
        refusal = str(error)

    assert messages == []
    assert requests == []
    assert refusal is not None
    assert "redirect refused" in refusal.lower()
    assert TOKEN not in refusal
    assert QUERY_NAME not in refusal
    assert QUERY_VALUE not in refusal


@pytest.mark.usefixtures("socket_enabled")
async def test_refuses_cross_origin_websocket_redirect() -> None:
    requests: list[str] = []
    messages: list[WSMessage] = []
    target = web.Application()
    target.router.add_get(
        "/api/websocket",
        partial(accept_websocket, requests=requests, messages=messages),
    )
    async with TestServer(target, host="127.0.0.1", port=0) as target_server:
        location = target_server.make_url(f"/api/websocket?{QUERY}")

        async def redirect(_request: web.Request) -> web.Response:
            raise web.HTTPTemporaryRedirect(location=location)

        origin = web.Application()
        origin.router.add_get("/api/websocket", redirect)
        async with TestServer(origin, host="127.0.0.1", port=0) as origin_server:
            await assert_redirect_refused(
                str(origin_server.make_url("/api/websocket")), requests, messages
            )


@pytest.mark.usefixtures("socket_enabled")
async def test_refuses_same_origin_websocket_redirect() -> None:
    requests: list[str] = []
    messages: list[WSMessage] = []

    async def redirect(_request: web.Request) -> web.Response:
        raise web.HTTPTemporaryRedirect(location=f"/other?{QUERY}")

    origin = web.Application()
    origin.router.add_get("/api/websocket", redirect)
    origin.router.add_get(
        "/other", partial(accept_websocket, requests=requests, messages=messages)
    )
    async with TestServer(origin, host="127.0.0.1", port=0) as server:
        await assert_redirect_refused(
            str(server.make_url("/api/websocket")), requests, messages
        )


@pytest.mark.usefixtures("socket_enabled")
async def test_authenticates_without_websocket_redirect() -> None:
    requests: list[str] = []
    messages: list[WSMessage] = []
    origin = web.Application()
    origin.router.add_get(
        "/api/websocket",
        partial(accept_websocket, requests=requests, messages=messages),
    )
    async with TestServer(origin, host="127.0.0.1", port=0) as server:
        async with (
            asyncio.timeout(5),
            connect_authenticated(str(server.make_url("/api/websocket")), TOKEN) as ws,
        ):
            assert not ws.closed

    assert requests == ["/api/websocket"]
    assert [message.json() for message in messages] == [
        {"type": "auth", "access_token": TOKEN}
    ]
