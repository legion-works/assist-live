import io
import json
import wave
from unittest.mock import AsyncMock

import pytest
from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd
from assist_live_client.protocol import ProtocolError
from homeassistant.components.tts import (
    TextToSpeechEntity,
    TTSAudioRequest,
    TTSAudioResponse,
)
from test_tts_forward import Fallback, entity, text
from test_tts_stream import capable


def wav_audio(rate=16000, width=2, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x02\x00" * channels * width)
    return output.getvalue()


class StreamingFallback(Fallback):
    extension = "wav"

    def __init__(self, audio=None):
        self.audio = wav_audio() if audio is None else audio
        self.received_text = []

    async def async_stream_tts_audio(self, request):
        self.received_text.extend([part async for part in request.message_gen])

        async def output():
            for offset in range(0, len(self.audio), 7):
                yield self.audio[offset : offset + 7]

        return TTSAudioResponse(self.extension, output())


async def test_fallback_gets_each_text_part_once(client, ledger):
    capable(client)
    fallback = StreamingFallback()
    reads = []

    async def parts():
        for part in ("first ", "second"):
            reads.append(part)
            yield part

    async def fail(turn, source, voice):
        assert await anext(source) == "first "
        raise ProtocolError("start failure")
        yield

    client.tts_stream = fail
    response = await entity(client, ledger, fallback).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, parts())
    )
    assert [part async for part in response.data_gen]
    assert reads == ["first ", "second"]
    assert fallback.received_text == ["first ", "second"]


def interrupted(client, completed=1):
    async def fail(turn, source, voice):
        await anext(source)
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        if completed:
            yield TTSSentenceEnd(0)
        raise ProtocolError("mid-audio failure")

    client.tts_stream = fail


@pytest.mark.parametrize(
    "parts,completed,expected",
    [
        (["First sentence. ", "Second sentence."], 1, "Second sentence."),
        (["First. Second! Third?"], 2, "Third?"),
        (["Dr. Example speaks. ", "Next."], 1, "Next."),
        (["U.S. example. Next."], 1, "Next."),
        (["At 3.14. Next."], 1, "Next."),
        (['First!" Next.'], 1, "Next."),
    ],
)
async def test_matching_fallback_continues_next_sentence_without_wav_header(
    client, ledger, hass, parts, completed, expected
):
    capable(client)

    async def source():
        for part in parts:
            yield part

    async def fail(turn, source, voice):
        await anext(source)
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        for index in range(completed):
            yield TTSSentenceEnd(index)
        raise ProtocolError("mid-audio failure")

    client.tts_stream = fail
    fallback = StreamingFallback()
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, source())
    )
    output = b"".join([part async for part in response.data_gen])
    assert output[44:] == b"\x01\x00" + wav_audio()[44:]
    assert fallback.received_text == [expected]
    assert "tts_failed" not in next(iter(ledger.records.values())).stages


@pytest.mark.parametrize(
    "case",
    ["rate", "width", "channels", "codec", "unparseable", "empty", "no_sentence"],
)
@pytest.mark.parametrize("notify", [False, True])
async def test_failed_continuation_reports_full_text_without_repeating_action(
    client, ledger, hass, case, notify
):
    capable(client)
    interrupted(client, completed=0 if case == "no_sentence" else 1)
    fallback = StreamingFallback(
        wav_audio(
            rate=22050 if case == "rate" else 16000,
            width=1 if case == "width" else 2,
            channels=2 if case == "channels" else 1,
        )
    )
    if case == "codec":
        fallback.extension = "mp3"
    if case == "unparseable":
        fallback.audio = b"not a WAV file"
    if case == "empty":
        fallback.audio = wav_audio()[:40] + bytes(4)
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    forward.notify_tts_failure = notify
    events, notifications, actions = [], [], []
    hass.bus.async_listen(
        "assist_live_tts_failed", lambda event: events.append(event.data)
    )

    async def notification(call):
        notifications.append(call.data)

    async def action(call):
        actions.append(call.data)

    hass.services.async_register("persistent_notification", "create", notification)
    hass.services.async_register("test", "operate", action)
    hass.services.async_register("conversation", "process", action)
    await hass.services.async_call("test", "operate", {"value": "once"}, blocking=True)
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    output = b"".join([part async for part in response.data_gen])
    await hass.async_block_till_done()
    assert output[44:] == b"\x01\x00"
    assert "tts_failed" in next(iter(ledger.records.values())).stages
    assert events == [
        {"satellite": "satellite-a", "text": "First sentence. Second sentence."}
    ]
    assert actions == [{"value": "once"}]
    assert len(notifications) == int(notify)
    if notify:
        assert notifications[0]["message"] == "First sentence. Second sentence."
    if case == "no_sentence":
        assert fallback.received_text == []


async def test_diagnostics_has_counts_and_buffering_without_content(
    client, ledger, hass
):
    from types import SimpleNamespace

    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    capable(client)
    interrupted(client, completed=0)
    forward = entity(client, ledger, StreamingFallback())
    forward.hass = hass
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert [part async for part in response.data_gen]
    entry = SimpleNamespace(entry_id="entry", data={"token": "planted-sensitive-value"})
    hass.data["assist_live"] = {
        "entry": SimpleNamespace(ledger=ledger, tts={"device": forward})
    }
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["failures"]["tts_failed"] == 1
    assert result["tts_fallback"] == {"streaming": 1, "full_file": 0}
    serialized = json.dumps(result)
    for forbidden in (
        "planted-sensitive-value",
        "First sentence",
        "Second sentence",
        "RIFF",
    ):
        assert forbidden not in serialized


async def test_full_file_fallback_is_reported(client, ledger, hass):
    from types import SimpleNamespace

    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    capable(client, tts=False)
    fallback = TextToSpeechEntity()
    fallback._attr_supported_languages = ["en"]
    fallback._attr_default_language = "en"
    fallback.async_get_tts_audio = AsyncMock(return_value=("wav", wav_audio()))
    forward = entity(client, ledger, fallback)
    response = await forward.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
    assert b"".join([part async for part in response.data_gen]) == wav_audio()
    assert (
        fallback.async_get_tts_audio.call_args.args[0]
        == "First sentence. Second sentence."
    )
    entry = SimpleNamespace(entry_id="entry")
    hass.data["assist_live"] = {
        "entry": SimpleNamespace(ledger=ledger, tts={"d": forward})
    }
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["tts_fallback"] == {"streaming": 0, "full_file": 1}


async def test_notification_option_defaults_off(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    form = await flow.async_step_user()
    values = form["data_schema"](
        {
            "url": "https://server.invalid",
            "token": "test",
            "fallback_stt": "stt.test",
            "fallback_tts": "tts.test",
        }
    )
    assert values["notify_tts_failure"] is False


async def test_streaming_fallback_first_audio_before_input_end(client, ledger):
    import asyncio

    release = asyncio.Event()
    read = []

    async def source():
        read.append("first")
        yield "First. "
        await release.wait()
        read.append("second")
        yield "Second."

    class Streaming(TextToSpeechEntity):
        _attr_supported_languages = ["en"]
        _attr_default_language = "en"

        async def async_stream_tts_audio(self, request):
            async def output():
                async for part in request.message_gen:
                    yield part.encode()

            return TTSAudioResponse("mp3", output())

    forward = entity(client, ledger, Streaming())
    response = await asyncio.wait_for(
        forward.async_stream_tts_audio(TTSAudioRequest("en", {}, source())), 1
    )
    assert await asyncio.wait_for(anext(response.data_gen), 1) == b"First. "
    assert read == ["first"]
    release.set()
    assert b"".join([part async for part in response.data_gen]) == b"Second."
    assert read == ["first", "second"]


@pytest.mark.parametrize("unknown_size", [False, True])
async def test_wav_pcm_parser_closes_and_strips_fragmented_header(unknown_size):
    from custom_components.assist_live.tts_fallback import compatible_pcm

    data = wav_audio()
    if unknown_size:
        data = data[:40] + b"\xff\xff\xff\xff" + data[44:]
    closed = []

    async def source():
        try:
            for byte in data:
                yield bytes([byte])
        finally:
            closed.append(True)

    result = b"".join(
        [
            part
            async for part in compatible_pcm(
                TTSAudioResponse("wav", source()), TTSFormat("pcm_s16le", 16000, 1, 2)
            )
        ]
    )
    assert result == b"\x02\x00\x02\x00"
    assert closed == [True]
