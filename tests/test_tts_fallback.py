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
from homeassistant.core import callback
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

    async def fail(turn, source, voice, *, text_positions=False):
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
    async def fail(turn, source, voice, *, text_positions=False):
        await anext(source)
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        if completed:
            yield TTSSentenceEnd(0, 15)
        raise ProtocolError("mid-audio failure")

    client.tts_stream = fail


@pytest.mark.parametrize(
    "full,positions,expected",
    [
        ("First sentence. Second sentence.", [15], "Second sentence."),
        ("First. Second! Third?", [6, 14], "Third?"),
        ("Dr. Example speaks. Next.", [11], "speaks. Next."),
        ("“Ready!” \tNext.", [8], "Next."),
        ("😀 First. Next.", [8], "Next."),
        ("𐐀 speaks. Next.", [9], "Next."),
        ("First. Next.", [0], "First. Next."),
    ],
)
@pytest.mark.parametrize("marker_before_audio", [False, True])
async def test_matching_fallback_uses_server_position_without_wav_header(
    client, ledger, hass, full, positions, expected, marker_before_audio
):
    capable(client)

    reads, requests = [], []

    async def source():
        for part in (full[:4], full[4:]):
            reads.append(part)
            yield part

    async def fail(turn, source, voice, *, text_positions=False):
        requests.append(text_positions)
        assert "".join([part async for part in source]) == full
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        if marker_before_audio:
            yield TTSSentenceEnd(0, positions[0])
        yield TTSAudio(b"\x01\x00")
        for index, position in enumerate(positions):
            if index == 0 and marker_before_audio:
                continue
            yield TTSSentenceEnd(index, position)
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
    assert reads == [full[:4], full[4:]]
    assert requests == [True]
    client.health_capabilities.assert_awaited_once_with(timeout=0.2)
    assert "tts_failed" not in next(iter(ledger.records.values())).stages


@pytest.mark.parametrize("case", ["legacy", "missing", "missing_after_position"])
async def test_missing_position_reports_full_text(client, ledger, hass, case):
    capable(client, text_positions=case != "legacy")
    requested = []

    async def fail(turn, source, voice, *, text_positions=False):
        requested.append(text_positions)
        assert [part async for part in source]
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        if case == "missing_after_position":
            yield TTSSentenceEnd(0, 15)
            yield TTSSentenceEnd(1, None)
        else:
            yield TTSSentenceEnd(0, None)
        raise ProtocolError("mid-audio failure")

    client.tts_stream = fail
    fallback = StreamingFallback()
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    events = []
    hass.bus.async_listen(
        "assist_live_tts_failed", callback(lambda event: events.append(event.data))
    )
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert b"".join([part async for part in response.data_gen])[44:] == b"\x01\x00"
    await hass.async_block_till_done()
    assert requested == [case != "legacy"]
    client.health_capabilities.assert_awaited_once_with(timeout=0.2)
    assert fallback.received_text == []
    assert events == [
        {"satellite": "satellite-a", "text": "First sentence. Second sentence."}
    ]
    assert "tts_failed" in next(iter(ledger.records.values())).stages
    assert ledger.late_server_failures == 0


async def test_oversized_position_from_client_reports_full_text(client, ledger, hass):
    import asyncio
    from types import SimpleNamespace

    from aiohttp import WSMessage, WSMsgType
    from assist_live_client import AssistLiveClient
    from assist_live_client.protocol import encode_audio, encode_control

    capable(client)
    sent = asyncio.Event()
    errors, requests = [], []

    class Socket:
        def __init__(self):
            self.index = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_json(self, value):
            if value["type"] == "negotiate":
                self.turn = value["turn_id"]
                requests.append(value)
            elif value["type"] == "end":
                sent.set()

        async def send_str(self, value):
            pass

        async def receive(self, **kwargs):
            if self.index:
                await asyncio.wait_for(sent.wait(), 1)
            messages = [
                encode_control("negotiated", self.turn, capabilities=["tts"]),
                encode_control(
                    "format",
                    self.turn,
                    codec="pcm_s16le",
                    rate=16000,
                    channels=1,
                    width=2,
                ),
                encode_audio(self.turn, 0, b"\x01\x00"),
                encode_control("sentence_end", self.turn, sentence=0, text_end=33),
                encode_control("end", self.turn),
            ]
            value = messages[self.index]
            self.index += 1
            kind = WSMsgType.BINARY if isinstance(value, bytes) else WSMsgType.TEXT
            return WSMessage(
                kind, value if isinstance(value, bytes) else json.dumps(value), ""
            )

    wire = AssistLiveClient(
        "https://server.invalid",
        "test",
        session=SimpleNamespace(ws_connect=lambda *args, **kwargs: Socket()),
    )

    async def stream(*args, **kwargs):
        try:
            async for item in wire.tts_stream(*args, **kwargs):
                yield item
        except ProtocolError as error:
            errors.append(str(error))
            raise

    client.tts_stream = stream
    fallback = StreamingFallback()
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    events = []
    hass.bus.async_listen(
        "assist_live_tts_failed", callback(lambda event: events.append(event.data))
    )
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert b"".join([part async for part in response.data_gen])[44:] == b"\x01\x00"
    await hass.async_block_till_done()
    assert requests[0].get("text_positions") is True
    assert errors == ["TTS text position out of range"]
    assert fallback.received_text == []
    assert events == [
        {"satellite": "satellite-a", "text": "First sentence. Second sentence."}
    ]
    assert "tts_failed" in next(iter(ledger.records.values())).stages
    assert ledger.late_server_failures == 0


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
        "assist_live_tts_failed", callback(lambda event: events.append(event.data))
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


@pytest.mark.parametrize("late", [False, True])
async def test_complete_reply_counts_only_late_server_errors(
    client, ledger, hass, caplog, late
):
    from types import SimpleNamespace

    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    capable(client)

    async def synth(turn, source, voice, *, text_positions=False):
        assert [part async for part in source] == [
            "First sentence. ",
            "Second sentence.",
        ]
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(0, 15)
        yield TTSAudio(b"\x02\x00")
        yield TTSSentenceEnd(1, 32)
        if late:
            raise ProtocolError("planted-sensitive-late-error")

    client.tts_stream = synth
    fallback = StreamingFallback()
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    forward.notify_tts_failure = True
    events, notifications = [], []
    hass.bus.async_listen(
        "assist_live_tts_failed", callback(lambda event: events.append(event.data))
    )

    async def notification(call):
        notifications.append(call.data)

    hass.services.async_register("persistent_notification", "create", notification)
    entry = SimpleNamespace(entry_id="entry")
    hass.data["assist_live"] = {
        "entry": SimpleNamespace(ledger=ledger, tts={"device": forward})
    }
    for count in (1, 2):
        response = await forward.async_stream_tts_audio(
            TTSAudioRequest("en", {"voice": "voice-2"}, text())
        )
        assert b"".join([part async for part in response.data_gen])[44:] == (
            b"\x01\x00\x02\x00"
        )
        await hass.async_block_till_done()
        result = await async_get_config_entry_diagnostics(hass, entry)
        assert result["late_server_failures"] == (count if late else 0)
        assert result["failures"] == {}
        assert all(
            "tts_failed" not in record.stages for record in ledger.records.values()
        )
        assert events == []
        assert notifications == []
        assert fallback.received_text == []
    records = [
        r for r in caplog.records if r.name == "custom_components.assist_live.tts"
    ]
    assert len(records) == (2 if late else 0)
    for record in records:
        assert record.getMessage() == "TTS stream failed: ProtocolError"
        assert record.exc_info is None
    assert "planted-sensitive-late-error" not in caplog.text


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
