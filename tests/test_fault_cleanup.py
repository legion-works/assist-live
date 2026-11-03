import asyncio
import json
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from test_pipeline_contract import pipeline_hass as configured_hass  # noqa: F401

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
    label = {
        "id": "clip",
        "audio_sha256": "a" * 64,
        "split": "held_out",
        **row["label"],
    }
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

    async def engines(*_):
        return row["stt_engines"]

    async def baselines(*_, **kwargs):
        return row["baselines"]

    monkeypatch.setattr(fault_runner, "baseline_engines", engines)
    monkeypatch.setattr(fault_runner, "collect_baselines", baselines)
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


@pytest.mark.usefixtures(
    "configured_hass", "enable_custom_integrations", "available_fallbacks"
)
async def test_actual_ha_options_discovery_keeps_entry_options_byte_identical(
    hass,
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain="assist_live",
        data={"fallback_stt": "stt.original", "fallback_tts": "tts.standard"},
        options={"fallback_stt": "stt.selected", "record_transcripts": False},
    )
    entry.add_to_hass(hass)
    before = json.dumps(dict(entry.options), separators=(",", ":")).encode()
    form = await hass.config_entries.options.async_init(entry.entry_id)
    try:
        fallback = next(
            key for key in form["data_schema"].schema if key == "fallback_stt"
        )
        assert fallback.default() == "stt.selected", "OPTIONS_OVERRIDE_ENTRY_DATA"
    finally:
        hass.config_entries.options.async_abort(form["flow_id"])
    after = json.dumps(dict(entry.options), separators=(",", ":")).encode()
    assert after == before, "HA_OPTIONS_BYTE_IDENTICAL_AFTER_DISCOVERY"
    assert not hass.config_entries.options.async_progress(), "HA_OPTIONS_FLOW_DELETED"


@pytest.mark.parametrize(
    "form_case",
    [
        "valid",
        "no-default",
        "denied",
        "non-mapping",
        "delete-fails-after-success",
        "delete-fails-after-error",
    ],
)
async def test_baseline_entity_uses_public_options_form_without_saved_change(
    form_case, tmp_path, socket_enabled, unused_tcp_port, monkeypatch
):
    options = b'{"fallback_stt":"stt.fallback","record_transcripts":false}'
    before = options
    flows = set()
    requests = []

    async def form(request):
        nonlocal options
        requests.append((request.method, request.path))
        assert request.headers["Authorization"] == "Bearer synthetic"
        if request.method == "DELETE":
            if form_case.startswith("delete-fails"):
                return web.Response(status=500)
            flows.remove(request.match_info["flow_id"])
            return web.json_response({"message": "Flow aborted"})
        assert await request.json() == {"handler": "entry"}
        if form_case == "denied":
            return web.Response(status=403)
        if form_case == "non-mapping":
            return web.json_response([])
        flows.add("flow")
        return web.json_response(
            {
                "type": "form",
                "flow_id": "flow",
                "data_schema": [
                    {
                        "name": "fallback_stt",
                        **(
                            {"default": json.loads(options)["fallback_stt"]}
                            if form_case in ("valid", "delete-fails-after-success")
                            else {}
                        ),
                    }
                ],
            }
        )

    @asynccontextmanager
    async def command(*_):
        async def call(kind):
            if kind == "assist_pipeline/pipeline/list":
                return {
                    "pipelines": [
                        {
                            "id": "trial",
                            "stt_engine": "stt.server",
                            "stt_language": "en",
                        }
                    ]
                }
            assert kind == "config/entity_registry/list"
            return [
                {
                    "entity_id": "stt.server",
                    "platform": "assist_live",
                    "config_entry_id": "entry",
                }
            ]

        yield call

    app = web.Application()
    app.router.add_post("/api/config/config_entries/options/flow", form)
    app.router.add_delete("/api/config/config_entries/options/flow/{flow_id}", form)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "localhost", unused_tcp_port).start()
    monkeypatch.setattr(fault_runner, "control_command", command)
    monkeypatch.setattr(fault_runner, "URL", f"http://localhost:{unused_tcp_port}")
    token_file = tmp_path / "authentication.json"
    token_file.write_text(json.dumps({"token": "synthetic"}))
    try:
        async with aiohttp.ClientSession() as session:
            if form_case == "valid":
                assert await fault_runner.baseline_engines(
                    session, token_file, "trial"
                ) == {
                    "server": "stt.server",
                    "fallback": "stt.fallback",
                    "language": "en",
                }
            elif form_case == "delete-fails-after-success":
                with pytest.raises(aiohttp.ClientResponseError):
                    await fault_runner.baseline_engines(session, token_file, "trial")
            elif form_case == "delete-fails-after-error":
                with pytest.raises(ValueError) as caught:
                    await fault_runner.baseline_engines(session, token_file, "trial")
                assert str(caught.value) == "fallback STT entity unavailable", (
                    "DELETE_FAILURE_KEEPS_FIRST_ERROR"
                )
                assert caught.value.__notes__ == [
                    "options flow delete failed: ClientResponseError"
                ], "DELETE_FAILURE_NOTED_ON_FIRST_ERROR"
            else:
                with pytest.raises((ValueError, aiohttp.ClientError)):
                    await fault_runner.baseline_engines(session, token_file, "trial")
        assert options == before, "OPTIONS_BYTE_IDENTICAL_AFTER_UNSUBMITTED_FLOW"
        if not form_case.startswith("delete-fails"):
            assert not flows, "OPTIONS_FLOW_ALWAYS_DELETED"
        assert [method for method, _ in requests] == (
            ["POST"] if form_case in ("denied", "non-mapping") else ["POST", "DELETE"]
        )
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("fail", [None, "server", "fallback", "discovery"])
async def test_collect_baselines_keeps_engine_and_clip_identity_and_fails_closed(
    fail, monkeypatch, tmp_path
):
    from tests.test_fault_runner import trial

    calls = []
    engines = {"server": "stt.server", "fallback": "stt.fallback", "language": "en"}
    label = {"id": "test", "audio_sha256": "a" * 64}
    pcm = b"complete synthetic clip"

    async def pipeline(*args, **kwargs):
        engine = "server" if args[2] == "temporary" else "fallback"
        assert args[3:5] == (label, pcm)
        assert args[5] == ("baseline" if engine == "server" else "fallback-baseline")
        assert kwargs["device_id"] == "virtual"
        calls.append(engine)
        row = trial()
        row["fallback_turns"][0]["destinations"] = []
        row.update(
            failures=["error"] if fail == engine else [],
            recognized_transcript=engine + " text",
        )
        return row

    @asynccontextmanager
    async def temporary(*args, **kwargs):
        assert kwargs["stt_engine"] == "stt.fallback"
        yield "fallback", "virtual", "sensor.turn"

    @asynccontextmanager
    async def control(*_):
        yield None

    monkeypatch.setattr(fault_runner, "pipeline_trial", pipeline)
    monkeypatch.setattr(fault_runner, "temporary_pipeline", temporary)
    monkeypatch.setattr(fault_runner, "control_command", control)
    token_file = tmp_path / "authentication.json"
    token_file.write_text('{"token":"synthetic"}')
    baselines = await fault_runner.collect_baselines(
        None,
        token_file,
        "temporary",
        label,
        pcm,
        {} if fail == "discovery" else engines,
        None,
        device_id="virtual",
        turn_sensor="sensor.turn",
    )
    assert calls == ([] if fail == "discovery" else ["server", "fallback"])
    for engine, record in baselines.items():
        assert record["clip_id"] == label["id"]
        assert record["audio_sha256"] == label["audio_sha256"]
        assert record["engine_id"] == (None if fail == "discovery" else engines[engine])
        assert record["status"] == (
            "unavailable" if fail in (engine, "discovery") else "available"
        )


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
