import asyncio
from contextlib import suppress
from unittest.mock import patch

import pytest
from assist_live_client.models import HealthCapabilities, TTSAudio, TTSFormat
from assist_live_client.protocol import ProtocolError
from homeassistant.components.tts import TTSAudioRequest
from test_stt_fallback import Clock
from test_stt_forward import Fallback as STTFallback
from test_stt_forward import audio, metadata
from test_stt_forward import entity as stt_entity
from test_tts_forward import Fallback as TTSFallback
from test_tts_forward import entity as tts_entity
from test_tts_forward import text


@pytest.mark.parametrize("path", ["stt", "tts", "connect"])
async def test_hanging_events_do_not_delay_turn_completion(
    client, ledger, monkeypatch, path
):
    clock = Clock(monkeypatch)
    entered = asyncio.Event()

    async def hang(event):
        entered.set()
        await asyncio.Event().wait()

    client.event.side_effect = hang
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def lost(turn_id, stream):
        await anext(stream)
        raise ProtocolError("closed", kind="closed")
        yield

    async def synth(turn_id, parts, voice, **kwargs):
        async for _ in parts:
            pass
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")

    async def run():
        if path == "stt":
            client.stt = lost
            result = await stt_entity(
                client, ledger, STTFallback()
            ).async_process_audio_stream(metadata(), audio([bytes(320)]))
            assert result.text == "test command"
        elif path == "tts":
            client.tts_stream = synth
            response = await tts_entity(
                client, ledger, TTSFallback()
            ).async_stream_tts_audio(
                TTSAudioRequest("en", {"voice": "voice-2"}, text())
            )
            assert b"".join([chunk async for chunk in response.data_gen])[44:] == (
                b"\x01\x00"
            )
        else:
            turn = ledger.open_voice("satellite-a")
            ledger.mark(turn.turn_id, "stt_final")
            assert await ledger.connect(turn)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await clock.move(0)
        assert task.done(), f"{path} waits for timing-event delivery"
        await task
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if stop := getattr(ledger, "stop_events", None):
            await stop()


@pytest.mark.parametrize("source", ["mark", "state"])
async def test_513th_event_drops_oldest_and_counts_it(ledger, source):
    turn = ledger.open_voice("satellite-a")
    oldest = None
    for _ in range(513):
        if source == "mark":
            ledger.mark(turn.turn_id, "stt_final")
        else:
            ledger.satellite_state("satellite-a", "idle")
        if oldest is None:
            oldest = ledger._pending[0]
    assert len(ledger._pending) == 512
    assert all(event is not oldest for event in ledger._pending)
    assert ledger.timing_events_dropped == 1


@pytest.mark.parametrize(
    "error", [ProtocolError("unavailable"), OSError(), TimeoutError()]
)
async def test_failed_event_stays_pending(client, ledger, error):
    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    ledger.mark(turn.turn_id, "stt_final")
    client.event.side_effect = error
    try:
        await ledger.flush()
    except OSError, TimeoutError:
        pass
    assert [event.stage for event in ledger._pending] == ["stt_final"]


async def test_each_event_request_expires_at_two_seconds(client, ledger, monkeypatch):
    clock = Clock(monkeypatch)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def hang(event):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client.event.side_effect = hang
    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    ledger.mark(turn.turn_id, "stt_final")
    ledger.schedule_events()
    try:
        await entered.wait()
        await clock.move(1.999)
        assert not cancelled.is_set()
        await clock.move(0.002)
        assert cancelled.is_set(), "timing request exceeded its 2 s limit"
        assert [event.stage for event in ledger._pending] == ["stt_final"]
        ledger.schedule_events()
        await clock.move(30)
        assert client.event.await_count == 1, "failed send retries without health"
    finally:
        await ledger.stop_events()


async def test_restart_marker_request_expires_at_two_seconds(
    client, ledger, monkeypatch
):
    clock = Clock(monkeypatch)
    entered = asyncio.Event()

    async def hang(event):
        entered.set()
        await asyncio.Event().wait()

    client.event.side_effect = hang
    rows = [
        {"turn_id": "01a00000-0000-7000-8000-000000000001", "satellite": "a"},
        {"turn_id": "01a00000-0000-7000-8000-000000000002", "satellite": "b"},
    ]
    task = asyncio.create_task(ledger.restore(rows))
    try:
        await entered.wait()
        await clock.move(1.999)
        assert not task.done()
        await clock.move(0.002)
        await clock.move(2.001)
        assert task.done(), "restart markers exceeded the 2 s request limit"
        assert task.exception() is None
        assert client.event.await_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_failed_batch_retries_only_after_new_healthy_maintenance(
    hass, client, monkeypatch
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime

    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    ledger = runtime.ledger
    now = [100.0]
    runtime.clock = ledger.clock = lambda: now[0]
    clock = Clock(monkeypatch)
    attempts = []

    async def send(event):
        attempts.append(event.stage)
        if len(attempts) == 1:
            raise ProtocolError("unavailable")

    client.event.side_effect = send
    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    ledger.mark(turn.turn_id, "fallback_used", reason="stream_closed")
    ledger.mark(turn.turn_id, "stt_final")
    ledger.schedule_events()
    await clock.move(0)
    assert attempts == ["fallback_used"]
    client.health_capabilities.return_value = HealthCapabilities(False)
    with patch(
        "homeassistant.components.assist_pipeline.async_get_pipelines", return_value=[]
    ):
        try:
            await runtime._maintenance(None)
            ledger.schedule_events()
            await clock.move(0)
            assert attempts == ["fallback_used"]
            client.health_capabilities.return_value = HealthCapabilities(True)
            now[0] = 104.999
            await runtime._maintenance(None)
            await clock.move(0)
            assert attempts == ["fallback_used"]
            ledger.device_event("satellite-a", turn.turn_id, "playback_end")
            now[0] = 116
            ledger.prune()
            assert turn.turn_id not in ledger.records
            assert len(ledger._pending) == 2, "record pruning lost retry events"
            await runtime._maintenance(None)
            await clock.move(0)
            assert attempts == ["fallback_used", "fallback_used", "stt_final"]
            assert not ledger._pending
        finally:
            await runtime.stop()


async def test_one_sender_keeps_order_during_inflight_overflow(
    client, ledger, monkeypatch
):
    clock = Clock(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    async def send(event):
        sent.append(event)
        entered.set()
        await release.wait()

    client.event.side_effect = send
    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    ledger.mark(turn.turn_id, "stt_final")
    ledger.schedule_events()
    try:
        await entered.wait()
        for _ in range(513):
            ledger.mark(turn.turn_id, "tts_first_text")
            ledger.schedule_events()
        expected = list(ledger._pending)
        assert len(expected) == 512
        assert ledger.timing_events_dropped == 2
        assert len(sent) == 1
        release.set()
        await clock.move(0)
        assert sent[1:] == expected
        assert not ledger._pending
    finally:
        await ledger.stop_events()


async def test_events_wait_for_server_open_without_blocking_other_turns(
    client, ledger, monkeypatch
):
    clock = Clock(monkeypatch)
    unopened = ledger.open_voice("satellite-a")
    ledger.mark(unopened.turn_id, "stt_final")
    opened = ledger.open_voice("satellite-b")
    ledger.mark(opened.turn_id, "stt_final")
    assert await ledger.connect(opened)
    try:
        await clock.move(0)
        assert [call.args[0].turn_id for call in client.event.call_args_list] == [
            opened.turn_id
        ]
        assert await ledger.connect(unopened)
        await clock.move(0)
        assert [call.args[0].turn_id for call in client.event.call_args_list] == [
            opened.turn_id,
            unopened.turn_id,
        ]
    finally:
        await ledger.stop_events()


async def test_stopped_ledger_does_not_accept_late_events(client, ledger):
    turn = ledger.open_voice("satellite-a")
    await ledger.stop_events()
    ledger.mark(turn.turn_id, "stt_final")
    ledger.satellite_state("satellite-a", "idle")
    ledger.schedule_events(retry=True)
    assert not ledger._pending
    client.event.assert_not_awaited()


async def test_diagnostics_count_timing_queue_overflow(hass, client):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    hass.data["assist_live"] = {entry.entry_id: runtime}
    turn = runtime.ledger.open_voice("satellite-a")
    for _ in range(513):
        runtime.ledger.mark(turn.turn_id, "stt_final")
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["timing_events_dropped"] == 1
