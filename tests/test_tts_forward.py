import pytest
from homeassistant.components.tts import (
    TextToSpeechEntity,
    TTSAudioRequest,
    TTSAudioResponse,
    Voice,
)
from homeassistant.exceptions import HomeAssistantError


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


async def test_tts_fallback_language_narrower_than_advertised(client, ledger):
    fallback = Fallback()
    forward = entity(client, ledger, fallback)
    fallback.supported_languages = ["zz"]
    assert "en" in forward.supported_languages
    assert "en" not in fallback.supported_languages
    with pytest.raises(HomeAssistantError, match="language unavailable"):
        await forward.async_stream_tts_audio(
            TTSAudioRequest("en", {"voice": "voice-2"}, text())
        )
    assert not hasattr(fallback, "text")
    client.health.assert_not_awaited()
    assert ledger.records == {}


async def test_unusable_tts_fallback_is_a_named_ha_error_and_repair(hass):
    from types import SimpleNamespace
    from unittest.mock import patch

    from homeassistant.helpers import issue_registry as ir
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live.tts import async_setup_entry

    entry = MockConfigEntry(
        domain="assist_live", data={"fallback_tts": "tts.standard", "satellites": {}}
    )
    hass.data["assist_live"] = {entry.entry_id: SimpleNamespace()}
    with patch(
        "custom_components.assist_live.tts.get_engine_instance", return_value=object()
    ):
        with pytest.raises(HomeAssistantError, match="fallback TTS unavailable"):
            await async_setup_entry(hass, entry, lambda entities: None)
    issue_id = f"{entry.entry_id}_fallback_tts_unavailable"
    assert ir.async_get(hass).async_get_issue("assist_live", issue_id)
    with patch(
        "custom_components.assist_live.tts.get_engine_instance", return_value=Fallback()
    ):
        await async_setup_entry(hass, entry, lambda entities: None)
    assert ir.async_get(hass).async_get_issue("assist_live", issue_id) is None


@pytest.mark.parametrize("part,count", [("a", 16384), ("é", 8192)])
async def test_tts_text_limit_is_utf8_bytes(client, ledger, part, count):
    async def source():
        yield part * count
        yield "x"

    with pytest.raises(HomeAssistantError, match="text limit"):
        await entity(client, ledger, Fallback()).async_stream_tts_audio(
            TTSAudioRequest("en", {"voice": "voice-2"}, source())
        )
    turn = ledger.current("satellite-a")
    assert "tts_failed" in ledger.records[turn.turn_id].stages


async def test_empty_tts_parts_do_not_fill_the_replay_buffer(client, ledger):
    from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd

    client.health.return_value = True
    seen = []

    async def source():
        for _ in range(20000):
            yield ""
        yield "text"

    async def synth(turn_id, parts, voice):
        seen.extend([part async for part in parts])
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x00\x00")
        yield TTSSentenceEnd(0)

    client.tts_stream = synth
    response = await entity(client, ledger, Fallback()).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, source())
    )
    assert b"".join([chunk async for chunk in response.data_gen])
    assert seen == ["text"]


async def test_fallback_gets_new_unconsumed_generator(client, ledger):
    from assist_live_client.protocol import ProtocolError

    fallback = Fallback()
    forward = entity(client, ledger, fallback)
    client.health.return_value = True

    from assist_live_client.models import TTSFormat

    async def fail(turn_id, source, voice):
        assert await anext(source) == "First sentence. "
        raise ProtocolError("test loss")
        yield TTSFormat("pcm_s16le", 16000, 1, 2)

    client.tts_stream = fail
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

    from assist_live_client.models import TTSFormat

    async def fail(turn_id, parts, voice):
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
        yield TTSFormat("pcm_s16le", 16000, 1, 2)

    client.tts_stream = fail
    fallback = Fallback()
    response = await entity(client, ledger, fallback).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, source())
    )
    assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
    assert fallback.text == "First. Second."


async def test_non_default_pipeline_voice_reaches_client(client, ledger):
    seen = []
    client.health.return_value = True

    from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd

    async def synth(turn_id, source, voice):
        seen.append(voice)
        seen.append("".join([part async for part in source]))
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(0)

    client.tts_stream = synth
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


@pytest.mark.parametrize("rate,byte_rate", [(22050, 44100), (16000, 32000)])
async def test_tts_wav_header_matches_declared_format(client, ledger, rate, byte_rate):
    import struct

    from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd

    client.health.return_value = True

    async def synth(turn_id, source, voice):
        assert voice == "voice-2"
        yield TTSFormat("pcm_s16le", rate, 1, 2)
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(0)

    client.tts_stream = synth
    response = await entity(client, ledger, Fallback()).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    result = b"".join([chunk async for chunk in response.data_gen])
    expected = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        0xFFFFFFFF,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        rate,
        byte_rate,
        2,
        16,
        b"data",
        0xFFFFFFFF,
    )
    assert result[:44] == expected
    assert result[44:] == b"\x01\x00"


async def test_audio_before_format_uses_fallback(client, ledger):
    from assist_live_client.models import TTSAudio

    client.health.return_value = True

    async def synth(turn_id, source, voice):
        yield TTSAudio(b"\x01\x00")

    client.tts_stream = synth
    fallback = Fallback()
    response = await entity(client, ledger, fallback).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
    assert fallback.text == "First sentence. Second sentence."


async def test_unsupported_tts_format_uses_fallback(client, ledger):
    from assist_live_client.models import TTSAudio, TTSFormat

    client.health.return_value = True

    async def synth(turn_id, source, voice):
        yield TTSFormat("unknown", 22050, 1, 2)
        yield TTSAudio(b"\x01\x00")

    client.tts_stream = synth
    fallback = Fallback()
    response = await entity(client, ledger, fallback).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
    assert fallback.text == "First sentence. Second sentence."


async def test_sentence_end_items_are_counted(client, ledger):
    from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd

    client.health.return_value = True

    async def synth(turn_id, source, voice):
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(0)
        yield TTSAudio(b"\x02\x00")
        yield TTSSentenceEnd(1)

    client.tts_stream = synth
    forward = entity(client, ledger, Fallback())
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert (
        b"".join([chunk async for chunk in response.data_gen])[-4:]
        == b"\x01\x00\x02\x00"
    )


async def test_sentence_count_state_does_not_accumulate(client, ledger):
    from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd
    from assist_live_client.protocol import ProtocolError

    client.health.return_value = True
    calls = 0

    async def synth(turn_id, source, voice):
        nonlocal calls
        calls += 1
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        if calls % 2 == 0:
            raise ProtocolError("test pre-audio failure")
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(0)

    client.tts_stream = synth
    forward = entity(client, ledger, Fallback())
    initial_dicts = {
        key: value.copy()
        for key, value in vars(forward).items()
        if isinstance(value, dict)
    }
    for index in range(100):
        response = await forward.async_stream_tts_audio(
            TTSAudioRequest("en", {"voice": "voice-2"}, text())
        )
        result = b"".join([chunk async for chunk in response.data_gen])
        assert (result == b"fallback") is (index % 2 == 1)
    assert {
        key: value for key, value in vars(forward).items() if isinstance(value, dict)
    } == initial_dicts


async def test_partial_server_audio_stops_without_repeating_reply(hass, client, ledger):
    from assist_live_client.models import TTSAudio, TTSFormat
    from assist_live_client.protocol import ProtocolError

    client.health.return_value = True
    seen = []
    hass.bus.async_listen(
        "assist_live_tts_failed", lambda event: seen.append(event.data)
    )

    async def fail(turn_id, source, voice):
        await anext(source)
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        raise ProtocolError("test loss")

    client.tts_stream = fail
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
