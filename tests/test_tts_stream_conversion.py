import io
import shutil
import wave

from assist_live_client.models import (
    HealthCapabilities,
    TTSAudio,
    TTSFormat,
    TTSSentenceEnd,
)
from homeassistant.components import tts
from homeassistant.components.tts.const import DATA_COMPONENT
from test_pipeline_contract import pipeline_hass as configured_hass  # noqa: F401

from custom_components.assist_live.tts import AssistLiveTTS


async def test_ha_tts_stream_conversion_keeps_streaming_wav_pcm(
    configured_hass,  # noqa: F811
    client,
    ledger,
    monkeypatch,
):
    assert shutil.which("ffmpeg") is not None, "FFMPEG_REQUIRED_FOR_HA_TTS_CONVERSION"
    hass = configured_hass
    pcm_chunks = (
        b"\x00\x80\xff\x7f" * 257,
        b"\x34\x12\xcc\xed" * 521,
        b"\x01\x00\xff\xff" * 193,
    )
    expected_pcm = b"".join(pcm_chunks)
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def synthesize(turn_id, parts, voice, *, text_positions=False):
        assert "".join([part async for part in parts]) == "Synthetic reply."
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        for chunk in pcm_chunks:
            yield TTSAudio(chunk)
        yield TTSSentenceEnd(0)

    client.tts_stream = synthesize
    forward = AssistLiveTTS(
        "satellite-conversion",
        "device-conversion",
        client,
        ledger,
        "tts.contract_tts",
        "conversion-entry",
    )
    await hass.data[DATA_COMPONENT].async_add_entities([forward])
    emitted = []
    stream_tts = forward.async_stream_tts_audio

    async def capture_output(request):
        response = await stream_tts(request)
        assert response.extension == "wav"

        async def capture_chunks():
            async for chunk in response.data_gen:
                emitted.append(chunk)
                yield chunk

        return tts.TTSAudioResponse(response.extension, capture_chunks())

    monkeypatch.setattr(forward, "async_stream_tts_audio", capture_output)
    conversion_calls = []
    convert_audio = tts._async_convert_audio

    def track_conversion(hass, from_extension, audio_input, **kwargs):
        conversion_calls.append((from_extension, kwargs))
        return convert_audio(hass, from_extension, audio_input, **kwargs)

    monkeypatch.setattr(tts, "_async_convert_audio", track_conversion)
    result = tts.async_create_stream(
        hass,
        forward.entity_id,
        language="en",
        options={
            "preferred_format": "wav",
            "preferred_sample_rate": 16000,
            "preferred_sample_channels": 1,
            "preferred_sample_bytes": 2,
        },
    )

    async def message():
        yield "Synthetic "
        yield "reply."

    try:
        assert result.supports_streaming_input
        result.async_set_message_stream(message())
        delivered = b"".join([chunk async for chunk in result.async_stream_result()])
        assert result.extension == "wav"
        assert result.content_type == "audio/x-wav"
        assert conversion_calls == [
            (
                "wav",
                {
                    "to_extension": "wav",
                    "to_sample_rate": 16000,
                    "to_sample_channels": 1,
                    "to_sample_bytes": 2,
                    "to_bitrate": None,
                },
            )
        ]
        with wave.open(io.BytesIO(delivered), "rb") as output:
            assert (
                output.getframerate(),
                output.getnchannels(),
                output.getsampwidth(),
            ) == (16000, 1, 2)
            assert output.getcomptype() == "NONE"
            delivered_pcm = output.readframes(len(expected_pcm) // 2 + 1)
            assert output.readframes(1) == b""
        assert delivered_pcm == expected_pcm, "HA_STREAM_PCM_CHANGED"
        with wave.open(io.BytesIO(b"".join(emitted)), "rb") as source:
            assert source.readframes(len(expected_pcm) // 2 + 1) == delivered_pcm
    finally:
        result.delete()
        await ledger.stop_events()
