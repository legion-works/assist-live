import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd
from assist_live_client.protocol import ProtocolError
from homeassistant.components.tts import TTSAudioRequest
from test_tts_forward import Fallback, entity, text


def capable(client, *, healthy=True, stt=True, tts=True, text_positions=True):
    client.health.return_value = healthy
    client.health_capabilities = AsyncMock(
        return_value=SimpleNamespace(
            healthy=healthy, stt=stt, tts=tts, text_positions=text_positions
        )
    )


async def test_first_audio_before_input_end(client, ledger):
    capable(client)
    release = asyncio.Event()
    read = []

    async def source():
        read.append(1)
        yield "First. "
        await release.wait()
        read.append(2)
        yield "Second."

    async def synth(turn, parts, voice, *, text_positions=False):
        assert await anext(parts) == "First. "
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(0)
        assert await anext(parts) == "Second."
        yield TTSAudio(b"\x02\x00")
        yield TTSSentenceEnd(1)

    client.tts_stream = synth
    response = await asyncio.wait_for(
        entity(client, ledger, Fallback()).async_stream_tts_audio(
            TTSAudioRequest("en", {"voice": "voice-2"}, source())
        ),
        1,
    )
    assert (await anext(response.data_gen))[:4] == b"RIFF"
    assert await anext(response.data_gen) == b"\x01\x00"
    assert read == [1]
    release.set()
    assert b"".join([part async for part in response.data_gen]) == b"\x02\x00"
    assert read == [1, 2]


@pytest.mark.parametrize("healthy,tts", [(False, True), (True, False), (False, False)])
async def test_tts_requires_health_and_capability(client, ledger, healthy, tts):
    capable(client, healthy=healthy, tts=tts)

    async def forbidden(*args, **kwargs):
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(0)

    client.tts_stream = forbidden
    response = await entity(client, ledger, Fallback()).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert b"".join([part async for part in response.data_gen]) == b"fallback"
    client.health_capabilities.assert_awaited_once_with(timeout=0.2)


async def test_health_budget_does_not_include_turn_connect(client, ledger):
    from test_stt_fallback import Clock

    capable(client)
    entered, release = asyncio.Event(), asyncio.Event()

    async def open_turn(turn):
        entered.set()
        await release.wait()

    async def synth(turn, parts, voice, *, text_positions=False):
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(0)

    client.open_turn.side_effect = open_turn
    client.tts_stream = synth
    from pytest import MonkeyPatch

    with MonkeyPatch.context() as monkeypatch:
        clock = Clock(monkeypatch)
        task = asyncio.create_task(
            entity(client, ledger, Fallback()).async_stream_tts_audio(
                TTSAudioRequest("en", {"voice": "voice-2"}, text())
            )
        )
        await entered.wait()
        await clock.move(0.3)
        release.set()
        response = await task
        assert b"".join([part async for part in response.data_gen])[44:] == b"\x01\x00"


@pytest.mark.parametrize("error", [ProtocolError, TimeoutError, OSError])
async def test_health_failure_uses_fallback(client, ledger, error):
    capable(client)
    client.health_capabilities.side_effect = error("unavailable")
    response = await entity(client, ledger, Fallback()).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert b"".join([part async for part in response.data_gen]) == b"fallback"


async def test_absent_voice_uses_advertised_default(client, ledger):
    capable(client)
    fallback = Fallback()
    fallback._attr_default_options = {"voice": "voice-1"}

    async def synth(turn, parts, voice, *, text_positions=False):
        assert voice == "voice-1"
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(0)

    client.tts_stream = synth
    response = await entity(client, ledger, fallback).async_stream_tts_audio(
        TTSAudioRequest("en", {}, text())
    )
    assert b"".join([part async for part in response.data_gen])[44:] == b"\x01\x00"


async def test_two_byte_text_at_character_limit_is_accepted(client, ledger):
    async def source():
        yield "é" * 16384

    fallback = Fallback()
    response = await entity(client, ledger, fallback).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, source())
    )
    assert b"".join([part async for part in response.data_gen]) == b"fallback"
    assert fallback.text == "é" * 16384


@pytest.mark.parametrize("mode", ["stt", "tts"])
async def test_unconfigured_client_capability_uses_fallback(mode, hass):
    from assist_live_client import AssistLiveClient

    from custom_components.assist_live.ledger import TurnLedger

    client = AsyncMock(spec=AssistLiveClient)
    ledger = TurnLedger(client)
    if mode == "tts":
        response = await entity(client, ledger, Fallback()).async_stream_tts_audio(
            TTSAudioRequest("en", {"voice": "voice-2"}, text())
        )
        assert b"".join([part async for part in response.data_gen]) == b"fallback"
    else:
        from test_stt_forward import Fallback as STTFallback
        from test_stt_forward import audio, metadata
        from test_stt_forward import entity as stt_entity

        result = await stt_entity(
            client, ledger, STTFallback()
        ).async_process_audio_stream(metadata(), audio([bytes(320)]))
        assert result.text == "test command"
    assert "fallback_used" in next(iter(ledger.records.values())).stages
    client.open_turn.assert_not_awaited()


async def test_stt_health_without_stt_capability_uses_fallback(client, ledger):
    from test_stt_forward import Fallback as STTFallback
    from test_stt_forward import audio, metadata
    from test_stt_forward import entity as stt_entity

    capable(client, stt=False)
    result = await stt_entity(client, ledger, STTFallback()).async_process_audio_stream(
        metadata(), audio([bytes(320)])
    )
    assert result.text == "test command"
    client.open_turn.assert_not_awaited()


@pytest.mark.parametrize("delay,server", [(0.199, True), (0.201, False)])
async def test_tts_health_deadline(client, ledger, monkeypatch, delay, server):
    from test_stt_fallback import Clock

    capable(client)
    clock = Clock(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def health(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(healthy=True, tts=True, stt=False, text_positions=False)

    async def synth(turn, parts, voice, *, text_positions=False):
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")
        yield TTSSentenceEnd(0)

    client.health_capabilities.side_effect = health
    client.tts_stream = synth
    task = asyncio.create_task(
        entity(client, ledger, Fallback()).async_stream_tts_audio(
            TTSAudioRequest("en", {"voice": "voice-2"}, text())
        )
    )
    await entered.wait()
    await clock.move(delay)
    release.set()
    response = await task
    output = b"".join([part async for part in response.data_gen])
    assert (output[44:] == b"\x01\x00") is server


async def test_start_protocol_failure_logs_type_without_content(client, ledger, caplog):
    capable(client)

    async def fail(*args, **kwargs):
        raise ProtocolError("planted-sensitive-protocol-content")
        yield

    client.tts_stream = fail
    response = await entity(client, ledger, Fallback()).async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert b"".join([part async for part in response.data_gen]) == b"fallback"
    records = [
        r for r in caplog.records if r.name == "custom_components.assist_live.tts"
    ]
    assert len(records) == 1
    assert records[0].getMessage() == "TTS stream failed: ProtocolError"
    assert records[0].exc_info is None
    assert "planted-sensitive-protocol-content" not in caplog.text
