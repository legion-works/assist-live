import asyncio
import time

import pytest
from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd
from assist_live_client.protocol import ProtocolError
from homeassistant.components.tts import TTSAudioRequest
from test_tts_fallback import StreamingFallback, wav_audio
from test_tts_forward import entity
from test_tts_stream import capable


@pytest.mark.parametrize("phase", ["continuation", "early_start"])
async def test_active_text_after_failure_still_continues(
    client, ledger, hass, monkeypatch, recovery_clock, phase
):
    from custom_components.assist_live import tts as integration_tts

    monkeypatch.setattr(integration_tts, "EARLY_START_DELAY", 0)
    capable(client)
    first = "First sentence. "
    rest = [f"Part {n}. " for n in range(3)]
    rate = 16000 if phase == "continuation" else integration_tts.EARLY_START_RATE

    async def source():
        yield first
        await recovery_clock.armed.wait()
        for index, part in enumerate(rest):
            if index == 1:
                time.sleep(0.3)
            if recovery_clock.advance(60):
                await asyncio.Future()
            yield part
            await asyncio.sleep(0)

    async def fail(turn, parts, voice, *, text_positions=False):
        assert await anext(parts) == first
        yield TTSFormat("pcm_s16le", rate, 1, 2)
        if phase == "early_start":
            await audio_started.wait()
            return
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(0, 15)
        raise ProtocolError("mid-audio failure")

    audio_started = asyncio.Event()
    client.tts_stream = fail
    fallback = StreamingFallback(wav_audio(rate=rate))
    forward = entity(client, ledger, fallback)
    forward.hass = hass

    async def output():
        response = await forward.async_stream_tts_audio(
            TTSAudioRequest("en", {}, source())
        )
        chunks = []
        async for part in response.data_gen:
            chunks.append(part)
            if len(chunks) == 2:
                audio_started.set()
        return b"".join(chunks)

    audio = await asyncio.wait_for(output(), 10)
    assert "tts_failed" not in ledger.latest["satellite-a"].stages
    text = "".join(rest).lstrip()
    if phase == "continuation":
        assert fallback.received_text == [text]
        assert audio[44:] == b"\x01\x00" + wav_audio(rate=rate)[44:]
    else:
        assert fallback.received_text == [first + text]
        assert audio[44:] == integration_tts.EARLY_START_PCM + wav_audio(rate=rate)[44:]
