import asyncio
import shutil
from contextlib import aclosing
from importlib.metadata import version
from unittest.mock import AsyncMock

import pytest
from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd
from assist_live_client.protocol import ProtocolError
from awesomeversion import AwesomeVersion
from homeassistant.components import tts
from homeassistant.components.tts import TTSAudioRequest
from homeassistant.components.tts.const import DATA_COMPONENT
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_pipeline_contract import pipeline_hass  # noqa: F401
from test_tts_fallback import StreamingFallback, wav_audio
from test_tts_forward import Fallback, entity, text
from test_tts_stream import capable

from custom_components.assist_live import Runtime
from custom_components.assist_live.diagnostics import async_get_config_entry_diagnostics

WAV_22050 = bytes.fromhex(
    "52494646 ffffffff 57415645 666d7420 10000000 0100 0100 22560000 "
    "44ac0000 0200 1000 64617461 ffffffff"
)
SILENCE = bytes(11026)
PCM = b"\x01\x00\xff\xff"
REPLY = "First sentence. Second sentence."


@pytest.fixture(autouse=True)
def early_deadline(monkeypatch):
    monkeypatch.setattr(
        "custom_components.assist_live.tts.EARLY_START_DELAY", 0.01, raising=False
    )


class HeldServer:
    def __init__(
        self, client, rate=22050, outcome="audio", first_text="First sentence. "
    ):
        capable(client)
        client.tts_stream = self.stream
        self.rate = rate
        self.outcome = outcome
        self.first_text = first_text
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.step = None

    async def stream(self, turn, parts, voice, *, text_positions=False):
        try:
            assert await anext(parts) == self.first_text
            yield TTSFormat("pcm_s16le", self.rate, 1, 2)
            self.step = asyncio.current_task()
            self.waiting.set()
            await self.release.wait()
            if self.outcome == "error":
                yield TTSSentenceEnd(0, 15)
                raise ProtocolError("server audio failed")
            if self.outcome == "refused":
                raise ProtocolError("audio refused", kind="refused")
            if self.outcome == "empty_then_audio":
                yield TTSAudio(b"")
            if self.outcome in {"audio", "empty_then_audio"}:
                yield TTSAudio(PCM)
        finally:
            self.closed.set()


def start(forward):
    return forward.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))


async def diagnostics(hass, client, ledger):
    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    entry.runtime_data = Runtime(hass, entry, client)
    entry.runtime_data.ledger = ledger
    return await async_get_config_entry_diagnostics(hass, entry)


@pytest.mark.parametrize("outcome", ["audio", "empty_then_audio"])
async def test_slow_22050_server_returns_before_first_audio(
    hass, client, ledger, outcome
):
    server = HeldServer(client, outcome=outcome)
    forward = entity(client, ledger, Fallback())
    response = await asyncio.wait_for(start(forward), 1)
    async with aclosing(response.data_gen) as output:
        assert response.extension == "wav"
        assert await anext(output) == WAV_22050
        assert await anext(output) == SILENCE
        assert server.waiting.is_set() and not server.closed.is_set()
        assert "tts_audio_received" not in ledger.latest["satellite-a"].stages
        server.release.set()
        assert [chunk async for chunk in output] == [PCM]
    assert server.closed.is_set()
    assert "tts_audio_received" in ledger.latest["satellite-a"].stages
    result = await diagnostics(hass, client, ledger)
    assert result["early_reply_starts"] == 1
    assert result["turns"][0]["reply_start"] == "early"
    assert result["turns"][0]["tts_audio_output"] == "early_start"


async def test_slow_turn_save_counts_toward_early_start_deadline(
    client, ledger, monkeypatch
):
    server = HeldServer(client)
    loop = asyncio.get_running_loop()
    clock = loop.time
    elapsed = 0.0
    waits = []
    wait = asyncio.wait

    async def save():
        nonlocal elapsed
        elapsed = 0.08

    async def track_wait(tasks, *, timeout):
        waits.append(timeout)
        return await wait(tasks, timeout=timeout)

    ledger.save = save
    monkeypatch.setattr(loop, "time", lambda: clock() + elapsed)
    monkeypatch.setattr(asyncio, "wait", track_wait)
    monkeypatch.setattr("custom_components.assist_live.tts.EARLY_START_DELAY", 0.04)
    response = await asyncio.wait_for(start(entity(client, ledger, Fallback())), 1)
    async with aclosing(response.data_gen) as output:
        assert await anext(output) == WAV_22050
        assert await anext(output) == SILENCE
        assert waits == [0]
        server.release.set()
        assert [chunk async for chunk in output] == [PCM]


async def test_fast_22050_server_keeps_original_wav_bytes(hass, client, ledger):
    server = HeldServer(client)
    server.release.set()
    response = await asyncio.wait_for(start(entity(client, ledger, Fallback())), 1)
    assert response.extension == "wav"
    assert [chunk async for chunk in response.data_gen] == [WAV_22050, PCM]
    result = await diagnostics(hass, client, ledger)
    assert result.get("early_reply_starts", 0) == 0
    assert result["turns"][0]["reply_start"] is None
    assert result["turns"][0]["tts_audio_output"] == "streamed"


async def test_slow_16000_server_waits_for_first_audio(client, ledger):
    server = HeldServer(client, rate=16000)
    async with asyncio.TaskGroup() as tasks:
        request = tasks.create_task(start(entity(client, ledger, Fallback())))
        try:
            await asyncio.wait_for(server.waiting.wait(), 1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(request), 0.05)
        finally:
            server.release.set()
    response = request.result()
    chunks = [chunk async for chunk in response.data_gen]
    assert len(chunks) == 2 and chunks[-1] == PCM
    assert getattr(ledger, "early_reply_starts", 0) == 0


@pytest.mark.parametrize("outcome", ["error", "eof"])
async def test_early_stream_fallback_appends_pcm_for_full_reply(
    hass, client, ledger, outcome, caplog
):
    server = HeldServer(client, outcome=outcome)
    fallback = StreamingFallback(wav_audio(rate=22050))
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    response = await asyncio.wait_for(start(forward), 1)
    async with aclosing(response.data_gen) as output:
        assert await anext(output) == WAV_22050
        assert await anext(output) == SILENCE
        result = await diagnostics(hass, client, ledger)
        assert result["turns"][0]["tts_audio_output"] == "early_start"
        server.release.set()
        assert b"".join([chunk async for chunk in output]) == fallback.audio[44:]
    record = ledger.latest["satellite-a"]
    assert "".join(fallback.received_text) == REPLY
    assert [row["part"] for row in record.destinations] == ["tts", "tts_fallback"]
    assert record.fallback_reasons["tts"] == (
        "stream_closed" if outcome == "eof" else "stream_error"
    )
    assert "TTS stream failed: ProtocolError" in caplog.messages
    assert {"tts_failed", "tts_audio_received"}.isdisjoint(record.stages)
    result = await diagnostics(hass, client, ledger)
    assert result["turns"][0]["reply_start"] == "early"
    assert result["turns"][0]["tts_audio_output"] == "in_stream_fallback"


async def test_late_fallback_labels_pcm_in_the_same_stream(hass, client, ledger):
    capable(client)

    async def fail(turn, parts, voice, *, text_positions=False):
        assert await anext(parts) == "First sentence. "
        yield TTSFormat("pcm_s16le", 22050, 1, 2)
        yield TTSAudio(PCM)
        yield TTSSentenceEnd(0, 15)
        raise ProtocolError("server audio failed")

    client.tts_stream = fail
    fallback = StreamingFallback(wav_audio(rate=22050))
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    response = await asyncio.wait_for(start(forward), 1)
    assert b"".join([chunk async for chunk in response.data_gen]) == (
        WAV_22050 + PCM + fallback.audio[44:]
    )
    assert fallback.received_text == ["Second sentence."]
    result = await diagnostics(hass, client, ledger)
    assert result["turns"][0]["tts_audio_output"] == "in_stream_fallback"


@pytest.mark.parametrize("outcome", ["eof", "refused"])
async def test_whitespace_reply_ends_after_silence_without_tts_failure(
    hass, client, ledger, outcome
):
    server = HeldServer(client, outcome=outcome, first_text=" \t")
    fallback = StreamingFallback()
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    events = []
    hass.bus.async_listen(
        "assist_live_tts_failed", callback(lambda event: events.append(event.data))
    )

    async def whitespace():
        yield " \t"

    response = await asyncio.wait_for(
        forward.async_stream_tts_audio(TTSAudioRequest("en", {}, whitespace())), 1
    )
    async with aclosing(response.data_gen) as output:
        assert await anext(output) == WAV_22050
        assert await anext(output) == SILENCE
        server.release.set()
        assert [chunk async for chunk in output] == []
    await hass.async_block_till_done()
    record = ledger.latest["satellite-a"]
    assert record.reply_start == "early"
    assert ledger.early_reply_starts == 1
    assert not record.fallback_reasons
    assert {"tts_failed", "tts_audio_received"}.isdisjoint(record.stages)
    assert not fallback.received_text
    assert not events


@pytest.mark.parametrize("fallback_failure", ["different_rate", "engine_error"])
async def test_early_stream_failed_fallback_reports_tts_failed(
    hass, client, ledger, fallback_failure
):
    server = HeldServer(client, outcome="error")
    fallback = StreamingFallback()
    if fallback_failure == "engine_error":
        fallback.async_stream_tts_audio = AsyncMock(
            side_effect=HomeAssistantError("fallback audio failed")
        )
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    events = []
    hass.bus.async_listen(
        "assist_live_tts_failed", callback(lambda event: events.append(event.data))
    )
    response = await asyncio.wait_for(start(forward), 1)
    async with aclosing(response.data_gen) as output:
        assert await anext(output) == WAV_22050
        assert await anext(output) == SILENCE
        server.release.set()
        assert [chunk async for chunk in output] == []
    await hass.async_block_till_done()
    assert events == [{"satellite": "satellite-a", "text": REPLY}]
    assert "tts_failed" in ledger.latest["satellite-a"].stages
    result = await diagnostics(hass, client, ledger)
    assert result["turns"][0]["tts_audio_output"] == "early_start"


@pytest.mark.parametrize("fallback_available", [True, False])
async def test_format_then_refusal_before_pcm_reports_chosen_output(
    hass, client, ledger, fallback_available
):
    server = HeldServer(client, outcome="refused")
    server.release.set()
    fallback = StreamingFallback()
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    if fallback_available:
        response = await asyncio.wait_for(start(forward), 1)
        assert b"".join([chunk async for chunk in response.data_gen]) == fallback.audio
        assert "".join(fallback.received_text) == REPLY
    else:
        fallback.async_stream_tts_audio = AsyncMock(
            side_effect=HomeAssistantError("fallback audio failed")
        )
        with pytest.raises(HomeAssistantError, match="fallback audio failed"):
            await asyncio.wait_for(start(forward), 1)
    result = await diagnostics(hass, client, ledger)
    turn = result["turns"][0]
    assert result.get("early_reply_starts", 0) == 0
    assert turn["audio_format"] == {
        "codec": "pcm_s16le",
        "rate": 22050,
        "channels": 1,
        "width": 2,
    }
    assert turn["tts_audio_output"] == ("fallback" if fallback_available else "unknown")


@pytest.mark.parametrize("read_count", [0, 1, 2])
async def test_close_early_stream_reaps_pending_step(client, ledger, read_count):
    server = HeldServer(client)
    response = await asyncio.wait_for(start(entity(client, ledger, Fallback())), 1)
    async with aclosing(response.data_gen) as output:
        for expected in [WAV_22050, SILENCE][:read_count]:
            assert await anext(output) == expected
        await asyncio.wait_for(server.waiting.wait(), 1)
        assert not server.closed.is_set() and not server.step.done()
    assert server.closed.is_set() and server.step.done()


@pytest.mark.parametrize(
    "flac",
    [
        pytest.param(False, id="wav"),
        pytest.param(
            True,
            id="flac",
            marks=pytest.mark.skipif(
                AwesomeVersion(version("homeassistant")) < AwesomeVersion("2026.11.0"),
                reason="The HA 2026.10 WAV input path buffers the silent PCM.",
            ),
        ),
    ],
)
async def test_ha_stream_starts_before_first_server_audio(
    pipeline_hass,  # noqa: F811
    client,
    ledger,
    io_timeout,
    flac,
):
    if flac:
        assert shutil.which("ffmpeg") is not None, (
            "FFMPEG_REQUIRED_FOR_HA_TTS_CONVERSION"
        )
    hass = pipeline_hass
    server = HeldServer(client)
    forward = entity(client, ledger, Fallback())
    forward._attr_name = "Early Start TTS"
    await hass.data[DATA_COMPONENT].async_add_entities([forward])
    result = tts.async_create_stream(
        hass,
        forward.entity_id,
        language="en",
        options={
            "preferred_format": "flac",
            "preferred_sample_rate": 48000,
            "preferred_sample_channels": 1,
            "preferred_sample_bytes": 2,
        }
        if flac
        else {"preferred_format": "wav"},
    )
    try:
        result.async_set_message_stream(text())
        async with aclosing(result.async_stream_result()) as output:
            try:
                first = await asyncio.wait_for(anext(output), io_timeout)
                if flac:
                    assert first.startswith(b"fLaC")
                else:
                    assert first == WAV_22050
                    assert await asyncio.wait_for(anext(output), io_timeout) == SILENCE
                assert server.waiting.is_set() and not server.closed.is_set()
                assert "tts_audio_received" not in ledger.latest["satellite-a"].stages
            finally:
                server.release.set()
                async with asyncio.timeout(io_timeout):
                    async for _ in result.async_stream_result():
                        pass
    finally:
        server.release.set()
        result.delete()
        await ledger.stop_events()


@pytest.mark.parametrize("after_return", [False, True])
async def test_cancel_tts_while_server_step_is_pending(
    client, ledger, monkeypatch, after_return
):
    server = HeldServer(client)
    forward = entity(client, ledger, Fallback())
    if after_return:
        response = await asyncio.wait_for(start(forward), 1)
        assert await anext(response.data_gen) == WAV_22050
        assert await anext(response.data_gen) == SILENCE
        pending = asyncio.create_task(anext(response.data_gen))
    else:
        monkeypatch.setattr(
            "custom_components.assist_live.tts.EARLY_START_DELAY", 5, raising=False
        )
        pending = asyncio.create_task(start(forward))
    try:
        await asyncio.wait_for(server.waiting.wait(), 1)
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert server.closed.is_set() and server.step.done()
