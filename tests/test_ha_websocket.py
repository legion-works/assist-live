import asyncio
import errno
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

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


@pytest.mark.parametrize(
    "failure,cancel_error",
    [
        (ValueError, None),
        (TimeoutError, None),
        (ValueError, ValueError),
        (ValueError, TimeoutError),
    ],
)
async def test_failed_text_phase_cancels_timers(
    failure, cancel_error, token_file, monkeypatch
):
    calls = []
    states = [
        {"entity_id": "light.virtual_light"},
        {"entity_id": "fan.virtual_fan"},
        {
            "entity_id": "conversation.assist_live_short",
            "attributes": {"virtual_device_id": "virtual-device"},
        },
    ]

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_json(self, frame):
            calls.append(frame)

        async def receive_json(self):
            if not calls:
                return {"ha_version": "2026.10.3"}
            frame = calls[-1]
            kind = frame["type"]
            if kind == "auth":
                return {"type": "auth_ok"}
            result = None
            if kind == "conversation/agent/list":
                if calls[-2].get("text") == "start a timer":
                    if failure is TimeoutError:
                        raise TimeoutError("drain failed")
                    return {
                        "id": frame["id"],
                        "type": "result",
                        "success": False,
                        "error": {"code": "drain_failed"},
                    }
                result = {
                    "agents": [
                        {"id": f"conversation.assist_live_{g}"}
                        for g in ("short", "long")
                    ]
                }
            elif kind == "get_states":
                result = states
            if cancel_error and frame.get("text") == "cancel all timers":
                if cancel_error is TimeoutError:
                    raise TimeoutError()
                return {"id": frame["id"], "type": "result", "success": False}
            return {
                "id": frame["id"],
                "type": "result",
                "success": True,
                "result": result,
            }

    socket = Socket()

    class Session:
        async def ws_connect(self, _url):
            return socket

    @asynccontextmanager
    async def session():
        yield Session()

    async def configure(*args, **kwargs):
        pass

    monkeypatch.setattr(test_environment, "ha_session", session)
    monkeypatch.setattr(test_environment, "configure_agent", configure)
    monkeypatch.setattr(test_environment, "validate_live_inventory", lambda *args: [])
    with pytest.raises(failure, match="drain[ _]failed") as caught:
        await test_environment.run_live(
            {"short": [{"id": "timer", "transcript": "start a timer"}]},
            token_file,
            None,
        )
    assert [
        (frame["text"], frame["device_id"]) for frame in calls if "text" in frame
    ] == [
        ("start a timer", "virtual-device"),
        ("cancel all timers", "virtual-device"),
    ], "TEXT_FAILURE_CANCELS_TIMERS"
    if cancel_error is TimeoutError:
        assert caught.value.__notes__ == ["timer cleanup failed: TimeoutError"]
    elif cancel_error is ValueError:
        cleanup_cause = "public command failed: conversation/process (unknown)"
        assert caught.value.__notes__ == [
            f"timer cleanup failed: ValueError: {cleanup_cause}"
        ]


@pytest.mark.parametrize("entry", ["run_live", "record_device"])
@pytest.mark.parametrize("alias", [False, True])
async def test_recorder_refuses_repository_output(entry, alias, tmp_path, monkeypatch):
    from tools import private_evidence

    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setattr(private_evidence, "ROOT", root)
    output_dir = root
    if alias:
        output_dir = tmp_path / "alias"
        output_dir.symlink_to(root, target_is_directory=True)
    output = output_dir / "records.jsonl"
    missing_token = tmp_path / "not-read.json"

    with pytest.raises(ValueError, match="outside the repository"):
        if entry == "run_live":
            await test_environment.run_live({}, missing_token, output)
        else:
            await test_environment.record_device(
                missing_token, output, 1, "run", "config"
            )
    assert not list(root.iterdir())


@pytest.mark.parametrize(
    "replace_failure,mask", [(False, 0o022), (False, 0o777), (True, 0o022)]
)
async def test_recorder_keeps_private_atomic_checkpoints(
    replace_failure, mask, token_file, tmp_path, monkeypatch
):
    frames = iter(
        [
            {"ha_version": "2026.10.3"},
            {"type": "auth_ok"},
            {"success": True},
            *[
                {"type": "event", "event": {"data": {"turn_id": ident}}}
                for ident in ("first", "second")
            ],
        ]
    )

    class Socket:
        async def send_json(self, _frame):
            pass

        async def receive_json(self):
            return next(frames)

    class Session:
        @asynccontextmanager
        async def ws_connect(self, _url):
            yield Socket()

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(test_environment, "ha_session", session)
    monkeypatch.setattr(test_environment, "validate_run_evidence", lambda *a, **kw: [])
    output = tmp_path / "records.jsonl"
    if replace_failure:
        replace = Path.replace

        def reject_second(source, target):
            if target == output and output.exists():
                assert source.parent == output.parent
                assert source.stat().st_mode & 0o777 == 0o600
                raise OSError(errno.ENOSPC, "synthetic replacement failure")
            return replace(source, target)

        monkeypatch.setattr(Path, "replace", reject_second)
        with pytest.raises(OSError, match="synthetic replacement failure"):
            await test_environment.record_device(token_file, output, 2, "run", "config")
        assert {path.name for path in tmp_path.iterdir()} == {
            token_file.name,
            output.name,
        }
    else:
        previous = os.umask(mask)
        try:
            assert (
                await test_environment.record_device(
                    token_file, output, 2, "run", "config"
                )
                == []
            )
        finally:
            os.umask(previous)
    assert output.stat().st_mode & 0o777 == 0o600, "PRIVATE_RECORDER_MODE"
    assert [
        json.loads(line)["turn_id"] for line in output.read_text().splitlines()
    ] == (["first"] if replace_failure else ["first", "second"])


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
