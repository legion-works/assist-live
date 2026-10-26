import pytest
from assist_live_client.models import HealthCapabilities
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
    client.health_capabilities.assert_not_awaited()
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


@pytest.mark.parametrize("part,count", [("a", 16384), ("é", 16384)])
async def test_tts_text_limit_is_characters(client, ledger, part, count):
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

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
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
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

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
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

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
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

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


@pytest.mark.parametrize(
    "rate,byte_rate",
    [(16000, 32000), (22050, 44100)],
)
async def test_tts_wav_header_matches_declared_format(client, ledger, rate, byte_rate):
    import struct

    from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

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


async def test_wav_header_pack_failure_uses_fallback(client, ledger, monkeypatch):
    from assist_live_client.models import TTSAudio, TTSFormat

    from custom_components.assist_live import tts as tts_module

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def synth(turn_id, source, voice):
        yield TTSFormat("pcm_s16le", 22050, 1, 2)
        yield TTSAudio(b"\x01\x00")

    def fail_pack(*args, **kwargs):
        raise struct.error("header pack failed")

    import struct

    monkeypatch.setattr(tts_module.struct, "pack", fail_pack)
    client.tts_stream = synth
    fallback = Fallback()
    response = await entity(client, ledger, fallback).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
    assert fallback.text == "First sentence. Second sentence."


async def test_audio_before_format_uses_fallback(client, ledger):
    from assist_live_client.models import TTSAudio

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def synth(turn_id, source, voice):
        yield TTSAudio(b"\x01\x00")

    client.tts_stream = synth
    fallback = Fallback()
    response = await entity(client, ledger, fallback).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
    assert fallback.text == "First sentence. Second sentence."


@pytest.mark.parametrize(
    "case",
    [
        "unknown_codec",
        "8_bit",
        "24_bit",
        "stereo",
        "rate_8000",
        "rate_192000",
        "rate_7999",
        "rate_192001",
        "rate_2200000000",
        "duplicate_format_before_audio",
        "sentence_end_before_format",
        "unknown_item",
        "second_format_after_audio",
    ],
)
async def test_unexpressible_tts_stream_is_refused(client, ledger, hass, case):
    from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    failures = []
    hass.bus.async_listen(
        "assist_live_tts_failed", lambda event: failures.append(event.data)
    )

    async def synth(turn_id, source, voice):
        base = TTSFormat("pcm_s16le", 22050, 1, 2)
        if case == "unknown_codec":
            yield TTSFormat("unknown", 22050, 1, 2)
        elif case == "8_bit":
            yield TTSFormat("pcm_s16le", 22050, 1, 1)
        elif case == "24_bit":
            yield TTSFormat("pcm_s16le", 22050, 1, 3)
        elif case == "stereo":
            yield TTSFormat("pcm_s16le", 22050, 2, 2)
        elif case == "rate_8000":
            yield TTSFormat("pcm_s16le", 8000, 1, 2)
        elif case == "rate_192000":
            yield TTSFormat("pcm_s16le", 192000, 1, 2)
        elif case == "rate_7999":
            yield TTSFormat("pcm_s16le", 7999, 1, 2)
        elif case == "rate_192001":
            yield TTSFormat("pcm_s16le", 192001, 1, 2)
        elif case == "rate_2200000000":
            yield TTSFormat("pcm_s16le", 2_200_000_000, 1, 2)
        elif case == "duplicate_format_before_audio":
            yield base
            yield base
        elif case == "sentence_end_before_format":
            yield TTSSentenceEnd(0)
        elif case == "unknown_item":
            yield object()
        elif case == "second_format_after_audio":
            yield base
            yield TTSAudio(b"\x01\x00")
            yield base
            return
        yield TTSAudio(b"\x01\x00")

    client.tts_stream = synth
    fallback = Fallback()
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    result = b"".join([chunk async for chunk in response.data_gen])
    if case == "second_format_after_audio":
        assert result[:4] == b"RIFF"
        assert result[44:] == b"\x01\x00"
        assert len(failures) == 1
        assert not hasattr(fallback, "text")
    else:
        assert result == b"fallback"
        assert fallback.text == "First sentence. Second sentence."
        assert failures == []


async def test_sentence_end_items_do_not_end_or_fail_stream(client, ledger, hass):
    from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    failures = []
    hass.bus.async_listen(
        "assist_live_tts_failed", lambda event: failures.append(event.data)
    )

    async def synth(turn_id, source, voice):
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSSentenceEnd(0)
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(1)
        yield TTSAudio(b"\x02\x00")
        yield TTSSentenceEnd(2)

    client.tts_stream = synth
    forward = entity(client, ledger, Fallback())
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert (
        b"".join([chunk async for chunk in response.data_gen])[-4:]
        == b"\x01\x00\x02\x00"
    )
    assert failures == []


async def test_partial_server_audio_stops_without_repeating_reply(hass, client, ledger):
    from assist_live_client.models import TTSAudio, TTSFormat
    from assist_live_client.protocol import ProtocolError

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
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


async def test_format_without_audio_uses_fallback(client, ledger):
    from assist_live_client.models import TTSFormat, TTSSentenceEnd

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def synth(turn_id, source, voice):
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSSentenceEnd(0)

    client.tts_stream = synth
    fallback = Fallback()
    response = await entity(client, ledger, fallback).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
    assert fallback.text == "First sentence. Second sentence."


@pytest.mark.parametrize("phase", ["pre_audio", "mid_audio", "header"])
async def test_unexpected_tts_failure_logs_only_type(
    client, ledger, hass, caplog, monkeypatch, phase
):
    import logging

    from assist_live_client.models import TTSAudio, TTSFormat

    from custom_components.assist_live import tts as tts_module

    secret = "planted-sensitive-tts-content"
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def synth(turn_id, source, voice):
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        if phase == "pre_audio":
            raise ValueError(secret)
        yield TTSAudio(b"\x01\x00")
        if phase == "mid_audio":
            raise ValueError(secret)

    def fail_pack(*args, **kwargs):
        raise ValueError(secret)

    if phase == "header":
        monkeypatch.setattr(tts_module.struct, "pack", fail_pack)
    client.tts_stream = synth
    fallback = Fallback()
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    with caplog.at_level(logging.WARNING, logger=tts_module.__name__):
        response = await forward.async_stream_tts_audio(
            TTSAudioRequest("en", {"voice": "voice-2"}, text())
        )
        result = b"".join([chunk async for chunk in response.data_gen])
    records = [r for r in caplog.records if r.name == tts_module.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "ValueError" in records[0].getMessage()
    assert records[0].exc_info is None
    assert secret not in caplog.text
    assert "First sentence" not in caplog.text
    assert (result == b"fallback") is (phase != "mid_audio")
