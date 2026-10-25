from homeassistant.components.tts import (
    TextToSpeechEntity,
    TTSAudioRequest,
    TTSAudioResponse,
    Voice,
)


class Fallback(TextToSpeechEntity):
    _attr_default_language = "en"
    _attr_supported_languages = ["en"]

    def async_get_supported_voices(self, language):
        return [Voice("voice-1", "One"), Voice("voice-2", "Two")]

    async def async_stream_tts_audio(self, request):
        self.text = "".join([text async for text in request.message_gen])
        self.voice = request.options["voice"]

        async def output():
            yield b"fallback"

        return TTSAudioResponse("wav", output())


async def text():
    yield "First sentence. "
    yield "Second sentence."


def entity(client, ledger, fallback):
    from custom_components.assist_live.tts import AssistLiveTTS

    return AssistLiveTTS("satellite-a", "device-a", client, ledger, fallback)


async def test_fallback_gets_new_unconsumed_generator(client, ledger):
    from assist_live_client.protocol import ProtocolError

    fallback = Fallback()
    forward = entity(client, ledger, fallback)
    client.health.return_value = True

    async def fail(turn_id, source, voice):
        assert await anext(source) == "First sentence. "
        raise ProtocolError("test loss")
        yield b""

    client.tts = fail
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
    assert fallback.text == "First sentence. Second sentence."
    assert fallback.voice == "voice-2"


async def test_sender_cancellation_keeps_future_text(client, ledger):
    import asyncio
    from contextlib import suppress

    from assist_live_client.protocol import ProtocolError

    reading, release = asyncio.Event(), asyncio.Event()
    client.health.return_value = True

    async def source():
        yield "First. "
        reading.set()
        await release.wait()
        yield "Second."

    async def fail(turn_id, parts, *, voice):
        async def send():
            async for _ in parts:
                pass

        sender = asyncio.create_task(send())
        await asyncio.wait_for(reading.wait(), 1)
        sender.cancel()
        with suppress(asyncio.CancelledError):
            await sender
        release.set()
        raise ProtocolError("test sender loss")
        yield

    client.tts = fail
    fallback = Fallback()
    response = await entity(client, ledger, fallback).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, source())
    )
    assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
    assert fallback.text == "First. Second."


async def test_non_default_pipeline_voice_reaches_client(client, ledger):
    seen = []
    client.health.return_value = True

    async def synth(turn_id, source, *, voice):
        seen.append(voice)
        seen.append("".join([part async for part in source]))
        yield b"\x01\x00"

    client.tts = synth
    forward = entity(client, ledger, Fallback())
    assert "voice" in forward.supported_options
    assert forward.async_get_supported_voices("en")[1].voice_id == "voice-2"
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    import io
    import wave

    assert response.extension == "wav"
    with wave.open(
        io.BytesIO(b"".join([chunk async for chunk in response.data_gen]))
    ) as wav:
        assert wav.getparams()[:3] == (1, 2, 16000)
        assert wav.readframes(1) == b"\x01\x00"
    assert seen == ["voice-2", "First sentence. Second sentence."]
    turn = ledger.current("satellite-a")
    assert turn.turn_type == "tts_only"
    assert {"tts_first_text", "tts_first_byte"} <= ledger.records[turn.turn_id].stages


async def test_partial_server_audio_stops_without_repeating_reply(hass, client, ledger):
    from assist_live_client.protocol import ProtocolError

    client.health.return_value = True
    seen = []
    hass.bus.async_listen(
        "assist_live_tts_failed", lambda event: seen.append(event.data)
    )

    async def fail(turn_id, source, *, voice):
        await anext(source)
        yield b"\x01\x00"
        raise ProtocolError("test loss")

    client.tts = fail
    fallback = Fallback()
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    result = b"".join([chunk async for chunk in response.data_gen])
    await hass.async_block_till_done()
    assert result[-2:] == b"\x01\x00"
    assert not hasattr(fallback, "text")
    assert seen == [
        {"satellite": "satellite-a", "text": "First sentence. Second sentence."}
    ]
