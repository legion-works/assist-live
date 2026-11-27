import asyncio
import json
from contextlib import asynccontextmanager
from functools import partial

import pytest
from aiohttp import WSMessage, WSMessageTypeError, web
from aiohttp.test_utils import TestServer

from tools import probe_pipelines
from tools.ha_websocket import HA_URL
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


@pytest.mark.parametrize("lost_on", ["create", "get", None])
async def test_live_probe_reconciles_pipeline_on_a_fresh_socket(
    tmp_path, monkeypatch, lost_on
):
    token_file = tmp_path / "credential.json"
    token_file.write_text(json.dumps({"token": TOKEN}))
    connections = []
    pipelines = {
        "existing": {"id": "existing", "name": "Pipeline contract probe same-name 1"}
    }
    monkeypatch.setattr(
        probe_pipelines, "uuid4", lambda: type("Identifier", (), {"hex": "same-name"})()
    )
    active_connections = set()

    class Socket:
        failed = False

        async def send_json(self, message):
            if self.failed:
                raise ConnectionResetError("socket closed")
            self.message = message

        async def receive_json(self):
            operation = self.message["type"].rsplit("/", 1)[-1]
            match operation:
                case "list":
                    result = {
                        "pipelines": list(pipelines.values()),
                        "preferred_pipeline": "existing",
                    }
                case "voices":
                    result = {"voices": [{"voice_id": "voice"}]}
                case "create":
                    key = self.message["name"].rsplit(" ", 1)[-1]
                    result = pipelines[key] = {
                        **{k: v for k, v in self.message.items() if k != "type"},
                        "id": key,
                    }
                case "update":
                    pipelines[self.message["pipeline_id"]]["tts_voice"] = self.message[
                        "tts_voice"
                    ]
                    result = None
                case "delete":
                    del pipelines[self.message["pipeline_id"]]
                    result = None
                case "get":
                    result = pipelines[self.message["pipeline_id"]]
                case _:
                    pytest.fail(f"unexpected operation {operation}")
            if operation == lost_on:
                self.failed = True
                raise WSMessageTypeError("response lost")
            return {"id": self.message["id"], "success": True, "result": result}

    @asynccontextmanager
    async def connect(url, token):
        assert not active_connections
        connections.append((url, token))
        socket = Socket()
        active_connections.add(socket)
        try:
            yield socket
        finally:
            active_connections.remove(socket)

    monkeypatch.setattr(probe_pipelines, "connect_authenticated", connect)
    args = (HA_URL, token_file, "stt.test", "tts.test", "voice")
    if lost_on is None:
        assert await probe_pipelines.run_live(*args) == []
    else:
        with pytest.raises(WSMessageTypeError, match="response lost"):
            await probe_pipelines.run_live(*args)
    assert set(pipelines) == {"existing"}, "probe pipeline was stranded"
    assert connections == [(HA_URL + "/api/websocket", TOKEN)] * 2
