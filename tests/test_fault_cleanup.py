import asyncio
import json
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from tests.test_fault_runner import cleanup_bench as cleanup_bench
from tools import fault_runner


@pytest.fixture
async def ha_socket(
    tmp_path, socket_enabled, unused_tcp_port, monkeypatch, cleanup_bench
):
    sockets = []
    deletes = []
    rejected = []
    closed = asyncio.Event()

    async def handler(request):
        ws = web.WebSocketResponse(heartbeat=0.02)
        await ws.prepare(request)
        sockets.append(ws)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.10.3"})
        assert (await ws.receive_json()) == {
            "type": "auth",
            "access_token": "synthetic",
        }
        await ws.send_json({"type": "auth_ok"})
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                break
            data = json.loads(message.data)
            kind = data.pop("type")
            identifier = data.pop("id")
            if kind.endswith("/delete"):
                deletes.append(ws)
                if rejected:
                    await ws.send_json(
                        {
                            "id": identifier,
                            "success": False,
                            "error": {"message": rejected.pop(0)},
                        }
                    )
                    continue
            result = await cleanup_bench["command"](kind, **data)
            await ws.send_json({"id": identifier, "success": True, "result": result})
        closed.set()
        return ws

    app = web.Application()
    app.router.add_get("/api/websocket", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", unused_tcp_port)
    await site.start()
    monkeypatch.setattr(fault_runner, "URL", f"http://localhost:{unused_tcp_port}")
    token_file = tmp_path / "authentication.json"
    token_file.write_text(json.dumps({"token": "synthetic"}))
    try:
        yield token_file, sockets, deletes, rejected, closed
    finally:
        for ws in sockets:
            await ws.close()
        await runner.cleanup()


async def test_idle_control_reader_answers_ha_heartbeat(ha_socket):
    token_file, sockets, _, _, _ = ha_socket
    async with aiohttp.ClientSession() as session:
        async with fault_runner.control_command(session, token_file) as command:
            await command("get_states")
            await asyncio.sleep(0.15)
            assert not sockets[0].closed
            assert await command("get_states")


async def test_cleanup_uses_fresh_authenticated_socket_after_idle_close(ha_socket):
    token_file, sockets, deletes, _, closed = ha_socket
    result = {}
    async with aiohttp.ClientSession() as session:
        async with await fault_runner.connect(session, token_file) as old:
            counter = 0

            async def command(kind, **fields):
                nonlocal counter
                counter += 1
                await old.send_json({"id": counter, "type": kind, **fields})
                return (await old.receive_json())["result"]

            async def fresh(kind, **fields):
                async with fault_runner.control_command(session, token_file) as new:
                    return await new(kind, **fields)

            async with fault_runner.temporary_pipeline(
                command, result, "existing", cleanup_command=fresh
            ):
                await asyncio.wait_for(closed.wait(), 1)
                assert sockets[0].closed
            assert result["bench_restored"] is True
    assert len(deletes) == 1
    assert deletes[0] is not sockets[0]


@pytest.mark.parametrize("failures", [1, 2])
async def test_each_failed_delete_records_scrubbed_error(ha_socket, failures):
    token_file, sockets, deletes, rejected, _ = ha_socket
    rejected.extend(["synthetic denial"] * failures)
    result = {}
    async with aiohttp.ClientSession() as session:
        async with fault_runner.control_command(session, token_file) as command:

            async def fresh(kind, **fields):
                async with fault_runner.control_command(session, token_file) as new:
                    return await new(kind, **fields)

            async def run():
                async with fault_runner.temporary_pipeline(
                    command, result, "existing", cleanup_command=fresh
                ):
                    pass

            if failures == 2:
                with pytest.raises(ValueError, match="DELETE_FAILED: temporary"):
                    await run()
            else:
                await run()
    assert len(deletes) == 2
    assert deletes[0] is not deletes[1] and sockets[0] not in deletes
    assert result["cleanup_attempt_errors"] == [
        {
            "attempt": i,
            "class": "ValueError",
            "message": "isolated bench control command failed: synthetic denial",
        }
        for i in range(1, failures + 1)
    ]


def test_cleanup_error_scrubs_tokens_urls_and_addresses():
    message = fault_runner.scrub_cleanup_error(
        ValueError(
            "short-secret token=example Bearer example https://example.invalid/path "
            "192.0.2.1:1234 [2001:db8::1] example.invalid"
        ),
        ("short-secret",),
    )
    assert message == "[token] [token] [token] [url] [address] [[address]] [address]"


async def test_runner_wires_fresh_cleanup_after_control_disconnect(
    ha_socket, monkeypatch, tmp_path
):
    from argparse import Namespace

    from tests.test_fault_runner import trial

    token_file, sockets, deletes, _, _ = ha_socket
    row = trial()
    label = {"id": "clip", "split": "held_out", **row["label"]}
    monkeypatch.setattr(fault_runner, "load_corpus", lambda _: [label])
    monkeypatch.setattr(fault_runner, "audio_input", lambda *_: b"synthetic")

    async def restore(*_):
        pass

    async def measure(*_):
        return 0.125

    async def pipeline_trial(*_, **kwargs):
        await sockets[0].close()
        return {**row, "acceptable": True, "failures": []}

    monkeypatch.setattr(fault_runner.TestServer, "restore", restore)
    monkeypatch.setattr(fault_runner.TestServer, "measure_stop_latency", measure)
    monkeypatch.setattr(fault_runner, "pipeline_trial", pipeline_trial)
    result = await fault_runner.run(
        Namespace(
            output=tmp_path / "result.json",
            simulate=True,
            check=True,
            labels=tmp_path / "labels.jsonl",
            native_matrix=None,
            stages="after-final",
            each=1,
            audio_root=tmp_path,
            token_file=token_file,
            temporary_agent_pipeline=True,
            pipeline="existing",
        )
    )
    assert result["bench_restored"] is True
    assert result["restored_healthy"] is True
    assert result["stop_latency_seconds"] == 0.125
    assert len(deletes) == 1 and deletes[0] is not sockets[0]


@pytest.mark.parametrize("payload", ["counts", "missing", "malformed", "denied"])
async def test_public_diagnostics_download_extracts_counts_or_missing_evidence(
    payload, tmp_path, socket_enabled, unused_tcp_port, monkeypatch
):
    requests = []

    async def download(request):
        assert request.headers["Authorization"] == "Bearer synthetic"
        requests.append(request.path)
        if payload == "denied":
            return web.Response(status=403)
        if payload == "malformed":
            return web.Response(text="not json", content_type="application/json")
        return web.json_response(
            {
                "data": {
                    "fallback_reasons": {"health_timeout": 4, "stream_closed": 2},
                    "timing_events_dropped": 3,
                    "counters_since_utc_ns": 100,
                    "turns": [
                        {
                            "turn_id": "turn",
                            "destinations": [
                                {
                                    "part": "stt_fallback",
                                    "fallback_reason": "stream_closed",
                                }
                            ],
                        }
                    ],
                }
            }
            if payload == "counts"
            else {"data": {}}
        )

    @asynccontextmanager
    async def command(*_):
        async def call(kind):
            if kind == "assist_pipeline/pipeline/list":
                return {"pipelines": [{"id": "trial", "stt_engine": "stt.test"}]}
            assert kind == "config/entity_registry/list"
            return [
                {
                    "entity_id": "stt.test",
                    "platform": "assist_live",
                    "config_entry_id": "entry",
                }
            ]

        yield call

    app = web.Application()
    app.router.add_get("/api/diagnostics/config_entry/entry", download)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "localhost", unused_tcp_port).start()
    monkeypatch.setattr(fault_runner, "control_command", command)
    monkeypatch.setattr(fault_runner, "URL", f"http://localhost:{unused_tcp_port}")
    token_file = tmp_path / "authentication.json"
    token_file.write_text(json.dumps({"token": "synthetic"}))
    try:
        async with aiohttp.ClientSession() as session:
            counts = await fault_runner.fallback_snapshot(session, token_file, "trial")
        assert counts == (
            {
                "fallback_reasons": {"health_timeout": 4, "stream_closed": 2},
                "timing_events_dropped": 3,
                "counters_since_utc_ns": 100,
                "turns": [
                    {
                        "turn_id": "turn",
                        "destinations": [
                            {
                                "part": "stt_fallback",
                                "fallback_reason": "stream_closed",
                            }
                        ],
                    }
                ],
            }
            if payload == "counts"
            else None
        )
        assert requests == ["/api/diagnostics/config_entry/entry"]
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("failure", [False, True])
async def test_stop_latency_measures_stop_not_restore_and_always_restores(
    monkeypatch, failure
):
    clock = 100.0
    calls = []
    server = fault_runner.TestServer()

    async def stop():
        nonlocal clock
        calls.append("stop")
        clock += 0.125
        if failure:
            raise ValueError("synthetic stop failure")

    async def restore():
        nonlocal clock
        calls.append("restore")
        clock += 10

    monkeypatch.setattr(server, "stop", stop)
    monkeypatch.setattr(server, "restore", restore)
    monkeypatch.setattr(fault_runner.time, "monotonic", lambda: clock)
    if failure:
        with pytest.raises(ValueError, match="synthetic stop failure"):
            await server.measure_stop_latency()
    else:
        assert await server.measure_stop_latency() == 0.125
    assert calls == ["stop", "restore"]
