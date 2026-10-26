from dataclasses import replace

import pytest
from assist_live_client.models import HealthCapabilities
from homeassistant.components import stt


class Fallback(stt.SpeechToTextEntity):
    supported_languages = ["en"]
    supported_formats = [stt.AudioFormats.WAV]
    supported_codecs = [stt.AudioCodecs.PCM]
    supported_bit_rates = [stt.AudioBitRates.BITRATE_16]
    supported_sample_rates = [stt.AudioSampleRates.SAMPLERATE_16000]
    supported_channels = [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(self, metadata, stream):
        self.audio = b"".join([chunk async for chunk in stream])
        return stt.SpeechResult("test command", stt.SpeechResultState.SUCCESS)


def metadata():
    return stt.SpeechMetadata(
        "en",
        stt.AudioFormats.WAV,
        stt.AudioCodecs.PCM,
        stt.AudioBitRates.BITRATE_16,
        stt.AudioSampleRates.SAMPLERATE_16000,
        stt.AudioChannels.CHANNEL_MONO,
    )


async def audio(chunks):
    for chunk in chunks:
        yield chunk


def entity(client, ledger, fallback):
    from custom_components.assist_live.stt import AssistLiveSTT

    return AssistLiveSTT("satellite-a", "device-a", client, ledger, fallback)


async def test_external_vad_never_changes(client, ledger):
    forward = entity(client, ledger, Fallback())
    assert forward.audio_processing.requires_external_vad is False
    client.health_capabilities.return_value = HealthCapabilities(False, False, False)
    result = await forward.async_process_audio_stream(metadata(), audio([bytes(320)]))
    assert result.text == "test command"
    assert forward.audio_processing.requires_external_vad is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("language", "zz"),
        ("format", "flac"),
        ("codec", "opus"),
        ("sample_rate", 48000),
        ("bit_rate", 8),
        ("channel", 2),
    ],
)
async def test_fallback_incompatible_before_input(client, ledger, field, value):
    fallback = Fallback()
    consumed = []

    async def source():
        consumed.append(True)
        yield bytes(320)

    result = await entity(client, ledger, fallback).async_process_audio_stream(
        replace(metadata(), **{field: value}), source()
    )
    assert result.result == stt.SpeechResultState.ERROR
    assert consumed == []
    assert not hasattr(fallback, "audio")


@pytest.mark.parametrize(
    "capability",
    [
        "supported_languages",
        "supported_formats",
        "supported_codecs",
        "supported_sample_rates",
        "supported_bit_rates",
        "supported_channels",
    ],
)
async def test_fallback_narrower_than_advertised_entity(client, ledger, capability):
    fallback = Fallback()
    forward = entity(client, ledger, fallback)
    setattr(fallback, capability, [])
    request = metadata()
    assert forward.check_metadata(request)
    assert not fallback.check_metadata(request)
    consumed = []

    async def source():
        consumed.append(True)
        yield bytes(320)

    result = await forward.async_process_audio_stream(request, source())
    assert result.result == stt.SpeechResultState.ERROR
    assert consumed == []
    assert not hasattr(fallback, "audio")
    client.health_capabilities.assert_not_awaited()


async def test_local_endpoint_limits(client, ledger, monkeypatch):
    class Vad:
        def Process10ms(self, chunk):
            return 1.0 if chunk[0] else 0.0

    monkeypatch.setattr("custom_components.assist_live.stt.MicroVad", Vad)
    fallback = Fallback()
    forward = entity(client, ledger, fallback)
    await forward.async_process_audio_stream(
        metadata(), audio([b"\x01\x00" * 160] * 30 + [bytes(320)] * 100)
    )
    assert len(fallback.audio) == 32000
    await forward.async_process_audio_stream(
        metadata(), audio([b"\x01\x00" * 160] * 1600)
    )
    assert len(fallback.audio) == 480000


async def test_server_final_and_midstream_replay(client, ledger):
    from assist_live_client.protocol import ProtocolError

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    fallback = Fallback()
    forward = entity(client, ledger, fallback)

    async def final(turn_id, stream):
        async for _ in stream:
            break
        yield {"type": "final", "text": "server command"}

    client.stt = final
    result = await forward.async_process_audio_stream(metadata(), audio([bytes(320)]))
    assert result.text == "server command"
    assert not hasattr(fallback, "audio")

    async def fail(turn_id, stream):
        await anext(stream)
        raise ProtocolError("test loss")
        yield

    client.stt = fail
    chunks = [bytes([i, 0]) * 160 for i in (1, 2, 3)]
    result = await forward.async_process_audio_stream(metadata(), audio(chunks))
    assert result.text == "test command"
    assert fallback.audio == b"".join(chunks)


async def test_sender_cancellation_does_not_cancel_live_input(client, ledger):
    import asyncio
    from contextlib import suppress

    from assist_live_client.protocol import ProtocolError

    reading, release = asyncio.Event(), asyncio.Event()
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def source():
        yield b"\x01\x00" * 160
        reading.set()
        await release.wait()
        yield b"\x02\x00" * 160

    async def fail(turn_id, stream):
        async def send():
            async for _ in stream:
                pass

        sender = asyncio.create_task(send())
        await asyncio.wait_for(reading.wait(), 1)
        sender.cancel()
        with suppress(asyncio.CancelledError):
            await sender
        raise ProtocolError("test sender loss")
        yield

    class LiveFallback(Fallback):
        async def async_process_audio_stream(self, metadata, stream):
            first = await anext(stream)
            release.set()
            self.audio = first + b"".join([chunk async for chunk in stream])
            return stt.SpeechResult("command", stt.SpeechResultState.SUCCESS)

    client.stt = fail
    fallback = LiveFallback()
    result = await entity(client, ledger, fallback).async_process_audio_stream(
        metadata(), source()
    )
    assert result.text == "command"
    assert fallback.audio == b"\x01\x00" * 160 + b"\x02\x00" * 160


async def test_fallback_failure_is_empty_and_not_retried(client, ledger):
    from homeassistant.exceptions import HomeAssistantError

    class Broken(Fallback):
        calls = 0

        async def async_process_audio_stream(self, metadata, stream):
            self.calls += 1
            raise HomeAssistantError("test failure")

    fallback = Broken()
    result = await entity(client, ledger, fallback).async_process_audio_stream(
        metadata(), audio([bytes(320)])
    )
    assert not result.text
    assert result.result == stt.SpeechResultState.ERROR
    assert fallback.calls == 1
