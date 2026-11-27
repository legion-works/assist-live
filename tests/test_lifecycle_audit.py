import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from assist_live_client.models import (
    HealthCapabilities,
    TTSAudio,
    TTSFormat,
    TTSSentenceEnd,
)
from assist_live_client.protocol import ProtocolError
from homeassistant.components import stt
from homeassistant.components.tts import TTSAudioRequest, TTSAudioResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from test_stt_forward import Fallback as STTFallback
from test_stt_forward import entity as stt_entity
from test_stt_forward import metadata
from test_stt_hints import advance, refresh
from test_stt_hints import hint_runtime as hint_runtime
from test_tts_forward import Fallback as TTSFallback
from test_tts_forward import entity as tts_entity
from test_tts_forward import text
from test_tts_stream import capable
from test_tts_voices import health


async def test_refused_recovery_open_cannot_connect_or_become_parent(client, ledger):
    entered, release = asyncio.Event(), asyncio.Event()

    async def refused(turn):
        entered.set()
        await release.wait()
        raise ProtocolError("refused", kind="refused")

    client.open_turn.side_effect = refused
    turn = ledger.open_voice("satellite-a")
    ledger.mark(turn.turn_id, "tts_first_text")
    ledger.schedule_events(retry=True)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        waiter = asyncio.create_task(ledger.connect(turn))
        asyncio.get_running_loop().call_soon(release.set)
        connected = await asyncio.wait_for(waiter, 1)
        await asyncio.wait_for(ledger._event_sender, 1)
        client.event.assert_awaited_once()
        assert not connected
        assert not await ledger.connect(turn)
        client.open_turn.assert_awaited_once()
        assert ledger.records[turn.turn_id].open_failure == "open_refused"
        ledger.device_event("satellite-a", turn.turn_id, "playback_end")
        assert ledger.open_voice("satellite-a").parent_turn_id is None
    finally:
        release.set()
        await ledger.stop_events()


async def test_hint_retry_rebuilds_after_health_error(hass, hint_runtime):
    runtime, _, state, entity_id = hint_runtime
    await refresh(hass, runtime)
    assert "synthetic alias" in state["requests"][0][1]
    state["requests"].clear()
    with (
        patch.object(runtime, "_maintenance"),
        patch.object(
            runtime.client, "health_capabilities", side_effect=OSError
        ) as failed_health,
    ):
        er.async_get(hass).async_update_entity(entity_id, aliases=["Replacement Alias"])
        await hass.async_block_till_done()
        await advance(hass, 6)
        failed_health.assert_awaited_once()
        assert not state["requests"]
    await refresh(hass, runtime)
    assert len(state["requests"]) == 2
    for _, names in state["requests"]:
        assert "replacement alias" in names
        assert "synthetic alias" not in names


@pytest.mark.parametrize("stalled", [False, True], ids=["error", "timeout"])
async def test_stt_source_close_keeps_final(client, ledger, monkeypatch, stalled):
    from custom_components.assist_live import stt as integration_stt

    monkeypatch.setattr(integration_stt, "AUDIO_CLOSE_TIMEOUT", 0.02, raising=False)
    client.health_capabilities.return_value = HealthCapabilities(True, True, False)
    close_started = asyncio.Event()

    async def source():
        try:
            yield bytes(480000)
        finally:
            close_started.set()
            if stalled:
                await asyncio.Future()
            raise OSError("source close")

    async def final(turn, parts):
        assert sum([len(part) async for part in parts]) == 480000
        yield {"type": "final", "text": "server command"}

    client.stt = final
    forward = stt_entity(client, ledger, STTFallback())
    try:
        result = await asyncio.wait_for(
            forward.async_process_audio_stream(metadata(), source()), 0.5
        )
        assert result == stt.SpeechResult(
            "server command", stt.SpeechResultState.SUCCESS
        )
        assert close_started.is_set()
    finally:
        await ledger.prepare_speaker_read("satellite-a")


@pytest.mark.parametrize("phase", ["health", "server", "fallback", "close"])
async def test_superseded_stt_does_not_return_command(client, ledger, phase):
    entered, release = asyncio.Event(), asyncio.Event()
    fallback = STTFallback()

    async def pause():
        entered.set()
        await release.wait()

    async def check_health(**kwargs):
        if phase == "health":
            await pause()
        use_server = phase in {"server", "close"}
        return HealthCapabilities(use_server, use_server, False)

    async def source():
        try:
            yield bytes(480000 if phase == "close" else 320)
        finally:
            if phase == "close":
                await pause()

    async def final(turn, parts):
        async for _ in parts:
            pass
        if phase == "server":
            await pause()
        yield {"type": "final", "text": "old server command"}

    async def local(metadata, parts):
        if phase == "fallback":
            await pause()
        return stt.SpeechResult("old fallback command", stt.SpeechResultState.SUCCESS)

    client.health_capabilities.side_effect = check_health
    client.stt = final
    fallback.async_process_audio_stream = AsyncMock(side_effect=local)
    forward = stt_entity(client, ledger, fallback)
    task = asyncio.create_task(forward.async_process_audio_stream(metadata(), source()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        old = ledger.current("satellite-a")
        ledger.open_voice("satellite-a")
        release.set()
        assert await asyncio.wait_for(task, 1) == stt.SpeechResult(
            "", stt.SpeechResultState.ERROR
        )
        if phase == "health":
            fallback.async_process_audio_stream.assert_not_awaited()
        if phase == "server":
            assert "stt_final_received" not in ledger.records[old.turn_id].stages
        if phase == "fallback":
            assert "stt_final" not in ledger.records[old.turn_id].stages
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await ledger.prepare_speaker_read("satellite-a")


@pytest.mark.parametrize("partial_audio", [False, True])
async def test_lazy_tts_fallback_failure_reports(hass, client, ledger, partial_audio):
    capable(client)
    events = []
    hass.bus.async_listen("assist_live_tts_failed", events.append)

    async def refused(*args, **kwargs):
        raise ProtocolError("server failed")
        yield

    class LazyFallback(TTSFallback):
        async def async_stream_tts_audio(self, request):
            async def output():
                assert "".join([part async for part in request.message_gen])
                if partial_audio:
                    yield b"partial"
                raise HomeAssistantError("fallback failed")

            return TTSAudioResponse("wav", output())

    client.tts_stream = refused
    forward = tts_entity(client, ledger, LazyFallback())
    forward.hass = hass
    forward.notify_tts_failure = True
    notify = AsyncMock()
    hass.services.async_register("persistent_notification", "create", notify)
    response = await forward.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
    with pytest.raises(HomeAssistantError, match="fallback failed"):
        async for _ in response.data_gen:
            pass
    await hass.async_block_till_done()
    assert "tts_failed" in ledger.latest["satellite-a"].stages
    assert [event.data["text"] for event in events] == [
        "First sentence. Second sentence."
    ]
    notify.assert_awaited_once()


async def test_tts_source_error_reaches_consumer_before_cleanup(hass, client, ledger):
    capable(client)
    continued, events = [], []
    failed = asyncio.Event()
    hass.bus.async_listen("assist_live_tts_failed", events.append)

    async def source():
        yield "prefix"
        await failed.wait()
        raise OSError("source failed")

    async def server(turn, parts, **kwargs):
        assert await anext(parts) == "prefix"
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        failed.set()
        try:
            await anext(parts)
        except StopAsyncIteration:
            continued.append("clean EOF")

    client.tts_stream = server
    fallback = TTSFallback()
    forward = tts_entity(client, ledger, fallback)
    forward.hass = hass
    response = await forward.async_stream_tts_audio(TTSAudioRequest("en", {}, source()))
    output = b"".join([chunk async for chunk in response.data_gen])
    assert not continued
    assert output[44:] == b"\x01\x00"
    await hass.async_block_till_done()
    assert "tts_failed" in ledger.latest["satellite-a"].stages
    assert [event.data["text"] for event in events] == ["prefix"]
    assert not hasattr(fallback, "text")


@pytest.mark.parametrize("phase", ["no_fallback", "continuation"])
async def test_tts_failure_bounds_stalled_text(
    hass, client, ledger, recovery_clock, phase
):
    capable(client)
    release, closed = asyncio.Event(), asyncio.Event()
    events = []
    hass.bus.async_listen("assist_live_tts_failed", events.append)

    async def source():
        try:
            yield "prefix"
            await release.wait()
        finally:
            closed.set()

    async def server(turn, parts, **kwargs):
        assert await anext(parts) == "prefix"
        if phase == "continuation":
            yield TTSFormat("pcm_s16le", 16000, 1, 2)
            yield TTSAudio(b"\x01\x00")
            yield TTSSentenceEnd(0, 3)
        raise ProtocolError("server failed")

    client.tts_stream = server
    fallback = TTSFallback()
    forward = tts_entity(client, ledger, fallback)
    forward.hass = hass
    if phase == "no_fallback":
        forward.resolve_fallback = lambda: None

    async def request():
        response = await forward.async_stream_tts_audio(
            TTSAudioRequest("en", {}, source())
        )
        return b"".join([chunk async for chunk in response.data_gen])

    task = asyncio.create_task(request())
    try:
        await asyncio.wait_for(recovery_clock.armed.wait(), 10)
        assert recovery_clock.advance(100)
        if phase == "continuation":
            assert (await asyncio.wait_for(task, 10))[44:] == b"\x01\x00"
        else:
            with pytest.raises(HomeAssistantError):
                await asyncio.wait_for(task, 10)
        assert closed.is_set()
        await hass.async_block_till_done()
        assert [event.data["text"] for event in events] == ["prefix"]
        assert "tts_failed" in ledger.latest["satellite-a"].stages
        assert not hasattr(fallback, "text")
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_fresh_equal_tts_voice_health_clears_refusal(hass, client, ledger):
    initial = health(("voice-1", "One", "en-US"))
    client.health_capabilities.return_value = initial
    forward = tts_entity(client, ledger, TTSFallback())
    forward.hass = hass
    forward.entity_id = "tts.assist_live_test"

    async def refuse(*args, **kwargs):
        raise ProtocolError("unknown_tts_voice")
        yield

    client.tts_stream = refuse
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-1"}, text())
    )
    assert [chunk async for chunk in response.data_gen] == [b"fallback"]
    issue_id = f"{forward.unique_id}_unknown_tts_voice_voice-1"
    issues = ir.async_get(hass)
    forward.async_get_supported_voices("en")
    assert issues.async_get_issue("assist_live", issue_id) is not None
    ledger.last_health = health(("voice-1", "One", "en-US"))
    assert ledger.last_health == initial and ledger.last_health is not initial
    forward.async_get_supported_voices("en")
    assert issues.async_get_issue("assist_live", issue_id) is None
