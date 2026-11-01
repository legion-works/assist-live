import asyncio
import logging
from dataclasses import replace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from assist_live_client import AssistLiveClient
from assist_live_client.models import (
    HealthCapabilities,
    TTSAudio,
    TTSFormat,
    TTSSentenceEnd,
)
from assist_live_client.protocol import ProtocolError, encode_control
from homeassistant.components.tts import TTSAudioRequest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_stt_forward import Fallback as STTFallback
from test_stt_forward import audio, metadata
from test_stt_forward import entity as stt_entity
from test_tts_fallback import StreamingFallback, wav_audio
from test_tts_forward import Fallback as TTSFallback
from test_tts_forward import entity as tts_entity
from test_tts_forward import text

from custom_components.assist_live import Runtime
from custom_components.assist_live.diagnostics import async_get_config_entry_diagnostics
from custom_components.assist_live.ledger import TurnLedger


def protocol_error(kind, message):
    return ProtocolError(message, kind=kind)


@pytest.mark.parametrize("part", ["stt", "tts"])
@pytest.mark.parametrize(
    "case,reason,error_class",
    [
        ("health_timeout", "health_timeout", "TimeoutError"),
        ("health_client_timeout", "health_timeout", "ProtocolError"),
        ("health_error", "health_error", "ProtocolError"),
        ("health_oserror", "health_error", "OSError"),
        ("unhealthy", "server_unhealthy", "None"),
        ("unsupported", "unsupported", "None"),
        ("policy", "not_local_only", "None"),
        ("destination", "destination_not_local", "None"),
        ("open_refused", "open_refused", "ProtocolError"),
        ("open_error", "open_error", "ProtocolError"),
        ("open_timeout", "open_error", "TimeoutError"),
        ("open_oserror", "open_error", "OSError"),
        ("stream_refused", "stream_refused", "ProtocolError"),
        ("unknown_voice", "stream_refused", "ProtocolError"),
        ("stream_closed", "stream_closed", "ProtocolError"),
        ("stream_end", "stream_closed", "None"),
        ("message_timeout", "message_timeout", "TimeoutError"),
        ("client_timeout", "message_timeout", "ProtocolError"),
        ("malformed", "stream_error", "ProtocolError"),
        ("stream_oserror", "stream_error", "OSError"),
    ],
)
async def test_each_fallback_has_reason_event_count_and_safe_log(
    hass, client, monkeypatch, caplog, part, case, reason, error_class
):
    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    ledger = runtime.ledger
    ledger.save = None
    ledger.fallback_policy = None
    hass.data["assist_live"] = {entry.entry_id: runtime}
    health = HealthCapabilities(
        True,
        True,
        True,
        local_only=True,
        destinations={"stt": "local", "tts": "local"},
    )
    sensitive = "http://server.invalid/?token=synthetic-private-token"
    if case.startswith("health_"):
        error_type = {
            "health_timeout": TimeoutError,
            "health_error": ProtocolError,
            "health_oserror": OSError,
            "health_client_timeout": lambda message: protocol_error("timeout", message),
        }[case]
        client.health_capabilities.side_effect = error_type(sensitive)
    elif case == "unhealthy":
        health = replace(health, healthy=False)
    elif case == "unsupported":
        health = replace(health, **{part: False})
        reason = f"{part}_unsupported"
    elif case in {"policy", "destination"}:
        ledger.local_only = True
        health = (
            replace(health, local_only=False)
            if case == "policy"
            else replace(health, destinations={part: "cloud"})
        )
    elif case.startswith("open_"):
        client.open_turn.side_effect = {
            "open_refused": protocol_error("refused", sensitive),
            "open_error": ProtocolError(sensitive),
            "open_timeout": TimeoutError(sensitive),
            "open_oserror": OSError(sensitive),
        }[case]
    client.health_capabilities.return_value = health
    stream_error = {
        "stream_refused": protocol_error("refused", sensitive),
        "unknown_voice": protocol_error("refused", "unknown_tts_voice"),
        "stream_closed": protocol_error("closed", sensitive),
        "message_timeout": TimeoutError(sensitive),
        "client_timeout": protocol_error("timeout", sensitive),
        "malformed": ProtocolError(sensitive),
        "stream_oserror": OSError(sensitive),
    }.get(case)

    async def failed_stream(*args, **kwargs):
        if stream_error is not None:
            raise stream_error
        return
        yield

    events = []
    flush = ledger.flush

    async def capture_events():
        events.extend(ledger._pending)
        await flush()

    monkeypatch.setattr(ledger, "flush", capture_events)
    client.stt = failed_stream
    client.tts_stream = failed_stream
    with caplog.at_level(logging.DEBUG, logger="custom_components.assist_live"):
        if part == "stt":
            forward = stt_entity(client, ledger, STTFallback())
            result = await forward.async_process_audio_stream(
                metadata(), audio([bytes(320)])
            )
            assert result.text == "test command"
        else:
            forward = tts_entity(client, ledger, TTSFallback())
            forward.hass = hass
            response = await forward.async_stream_tts_audio(
                TTSAudioRequest("en", {}, text())
            )
            assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
    fallback_events = [event for event in events if event.stage == "fallback_used"]
    assert len(fallback_events) == 1
    assert fallback_events[0].data["reason"] == reason
    assert ledger.latest["satellite-a"].timings["fallback_used"]["reason"] == reason
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["fallback_reasons"][reason] == 1
    assert sum(diagnostics["fallback_reasons"].values()) == 1
    logs = [
        record for record in caplog.records if "fallback_used" in record.getMessage()
    ]
    assert len(logs) == 1
    if part == "stt" and case == "stream_end":
        error_class = "StopAsyncIteration"
    assert logs[0].getMessage() == f"fallback_used reason={reason} error={error_class}"
    assert logs[0].exc_info is None
    assert sensitive not in caplog.text
    assert "server.invalid" not in caplog.text
    assert "synthetic-private-token" not in caplog.text


async def test_tts_header_failure_has_stream_error_reason(client, ledger, monkeypatch):
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def synth(*args, **kwargs):
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x00\x00")

    def fail_pack(*args):
        raise ValueError("synthetic header failure")

    client.tts_stream = synth
    monkeypatch.setattr("custom_components.assist_live.tts.struct.pack", fail_pack)
    response = await tts_entity(client, ledger, TTSFallback()).async_stream_tts_audio(
        TTSAudioRequest("en", {}, text())
    )
    assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
    assert (
        ledger.latest["satellite-a"].timings["fallback_used"]["reason"]
        == "stream_error"
    )


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("refused", "stream_refused"),
        ("closed", "stream_closed"),
        ("timeout", "message_timeout"),
        ("malformed", "stream_error"),
    ],
)
async def test_tts_continuation_keeps_failure_reason(
    client, ledger, caplog, kind, reason
):
    client.health_capabilities.return_value = HealthCapabilities(True, True, True, True)

    async def synth(turn_id, parts, voice, *, text_positions=False):
        assert (
            "".join([part async for part in parts])
            == "First sentence. Second sentence."
        )
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(0, 15)
        raise protocol_error(kind, "synthetic-sensitive-stream-content")

    client.tts_stream = synth
    fallback = StreamingFallback()
    with caplog.at_level(logging.DEBUG, logger="custom_components.assist_live"):
        response = await tts_entity(client, ledger, fallback).async_stream_tts_audio(
            TTSAudioRequest("en", {}, text())
        )
        output = b"".join([chunk async for chunk in response.data_gen])
    assert output[44:] == b"\x01\x00" + wav_audio()[44:]
    assert fallback.received_text == ["Second sentence."]
    events = [call.args[0] for call in client.event.call_args_list]
    event = next(event for event in events if event.stage == "fallback_used")
    assert event.data["reason"] == reason
    assert ledger.fallback_reasons[reason] == 1
    assert "synthetic-sensitive-stream-content" not in caplog.text
    assert f"fallback_used reason={reason} error=ProtocolError" in caplog.text


@pytest.mark.parametrize("part", ["stt", "tts"])
@pytest.mark.parametrize(
    "cause,reason",
    [
        ("health_timeout", "health_timeout"),
        ("open_refused", "open_refused"),
        ("stream_refused", "stream_refused"),
        ("stream_closed", "stream_closed"),
        ("message_timeout", "message_timeout"),
        ("malformed", "stream_error"),
    ],
)
async def test_pinned_client_kinds_reach_fallback_events(
    socket_enabled, part, cause, reason
):
    release = asyncio.Event()

    async def health(request):
        if cause == "health_timeout":
            await release.wait()
        return web.json_response(
            {"version": 1, "healthy": True, "capabilities": {"stt": True, "tts": True}}
        )

    async def opened(request):
        return web.json_response({}, status=400 if cause == "open_refused" else 200)

    async def event(request):
        return web.json_response({})

    async def stream(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hello = await ws.receive_json()
        turn_id = hello["turn_id"]
        await ws.send_json(encode_control("negotiated", turn_id, capabilities=[part]))
        if cause == "stream_refused":
            await ws.send_json(
                encode_control("error", turn_id, code="synthetic_refusal")
            )
        elif cause == "malformed":
            await ws.send_str("{}")
        elif cause == "message_timeout":
            await release.wait()
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/v1/turns", opened)
    app.router.add_post("/v1/event", event)
    app.router.add_get("/v1/audio", stream)
    async with TestServer(app) as server:
        async with AssistLiveClient(str(server.make_url("")), "test-only") as client:
            ledger = TurnLedger(client)
            try:
                if part == "stt":
                    result = await stt_entity(
                        client, ledger, STTFallback()
                    ).async_process_audio_stream(metadata(), audio([bytes(320)]))
                    assert result.text == "test command"
                else:
                    response = await tts_entity(
                        client, ledger, TTSFallback()
                    ).async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
                    assert (
                        b"".join([chunk async for chunk in response.data_gen])
                        == b"fallback"
                    )
                assert (
                    ledger.latest["satellite-a"].timings["fallback_used"]["reason"]
                    == reason
                )
                assert ledger.fallback_reasons[reason] == 1
            finally:
                release.set()


async def test_fallback_for_pruned_turn_does_not_raise():
    class Client:
        pass

    ledger = TurnLedger(Client())
    await ledger.use_fallback(
        "01a00000-0000-7000-8000-000000000000",
        "stt_fallback",
        "stt.fallback",
        reason="stream_closed",
    )
    assert ledger.fallback_reasons["stream_closed"] == 0
    assert ledger.records == {}
