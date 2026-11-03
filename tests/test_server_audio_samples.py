import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import aiohttp
import pytest
from assist_live_client import AssistLiveClient
from assist_live_client.models import HealthCapabilities
from assist_live_client.protocol import decode_audio, encode_control
from homeassistant.components import stt
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_stt_forward import Fallback, audio, entity, metadata

from custom_components.assist_live import Runtime
from custom_components.assist_live.diagnostics import async_get_config_entry_diagnostics


def runtime_for(hass, client):
    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    runtime.ledger.save = None
    runtime.ledger.fallback_policy = None
    hass.data["assist_live"] = {entry.entry_id: runtime}
    return entry, runtime.ledger


async def turn_row(hass, entry, turn_id):
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    return next(row for row in diagnostics["turns"] if row["turn_id"] == turn_id)


async def test_unavailable_server_records_zero_samples(hass, client):
    entry, ledger = runtime_for(hass, client)
    result = await entity(client, ledger, Fallback()).async_process_audio_stream(
        metadata(), audio([bytes(320), bytes(640)])
    )
    assert result.result == stt.SpeechResultState.SUCCESS
    turn_id = ledger.latest["satellite-a"].turn.turn_id
    row = await turn_row(hass, entry, turn_id)
    assert row.get("server_audio_samples") == 0, "unavailable_server_sent_no_samples"
    assert row.get("server_audio_last_chunk_samples") == 0, "unavailable_has_no_chunk"
    assert type(row["server_audio_samples"]) is int


async def test_midstream_samples_exclude_buffer_and_fallback_continuation(hass, client):
    entry, ledger = runtime_for(hass, client)
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    first_taken, second_taken, buffered, continuation = (
        asyncio.Event() for _ in range(4)
    )

    async def source():
        yield bytes(2)
        await first_taken.wait()
        yield bytes(6)
        await second_taken.wait()
        yield bytes(10)
        yield bytes(14)
        buffered.set()
        await continuation.wait()
        yield bytes(18)

    async def fail(turn_id, stream):
        assert len(await anext(stream)) == 2
        first_taken.set()
        assert len(await anext(stream)) == 6
        second_taken.set()
        await buffered.wait()
        raise OSError("synthetic stream loss")
        yield

    class ContinuingFallback(Fallback):
        async def async_process_audio_stream(self, metadata, stream):
            self.turn_id = ledger.latest["satellite-a"].turn.turn_id
            self.before = await turn_row(hass, entry, self.turn_id)
            continuation.set()
            return await super().async_process_audio_stream(metadata, stream)

    client.stt = fail
    fallback = ContinuingFallback()
    async with asyncio.timeout(2):
        result = await entity(client, ledger, fallback).async_process_audio_stream(
            metadata(), source()
        )
    assert result.result == stt.SpeechResultState.SUCCESS
    assert fallback.before.get("server_audio_samples") == 4, (
        "only_handed_over_samples_not_bytes_or_buffer"
    )
    assert fallback.before.get("server_audio_last_chunk_samples") == 3, (
        "last_handed_over_chunk_sets_write_bound"
    )
    assert len(fallback.audio) == 50, "fallback_keeps_unsent_and_continued_audio"
    row = await turn_row(hass, entry, fallback.turn_id)
    assert row.get("server_audio_samples") == 4, "fallback_replay_does_not_add_samples"
    assert row.get("server_audio_last_chunk_samples") == 3, (
        "fallback_replay_does_not_change_write_bound"
    )
    assert ledger.records[fallback.turn_id].server_audio_samples == 4
    assert any(
        item.get("fallback_reason") == "stream_error" for item in row["destinations"]
    )

    client.health_capabilities.return_value = HealthCapabilities(False, False, False)
    await entity(client, ledger, Fallback()).async_process_audio_stream(
        metadata(), audio([bytes(22)])
    )
    second_id = ledger.latest["satellite-a"].turn.turn_id
    assert second_id != fallback.turn_id
    second = await turn_row(hass, entry, second_id)
    assert second.get("server_audio_samples") == 0, "sample_count_is_per_turn"
    assert second.get("server_audio_last_chunk_samples") == 0, "chunk_bound_is_per_turn"
    assert ledger.records[fallback.turn_id].server_audio_samples == 4


async def test_successful_server_turn_has_no_fallback_sample_field(hass, client):
    entry, ledger = runtime_for(hass, client)
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def final(turn_id, stream):
        async for _ in stream:
            pass
        yield {"type": "final", "text": "server command"}

    client.stt = final
    forward = entity(client, ledger, Fallback())
    result = await forward.async_process_audio_stream(metadata(), audio([bytes(10)]))
    assert result.text == "server command"
    turn_id = ledger.latest["satellite-a"].turn.turn_id
    row = await turn_row(hass, entry, turn_id)
    assert "server_audio_samples" not in row, "no_stt_fallback_no_sample_field"
    assert "server_audio_last_chunk_samples" not in row, "no_fallback_no_chunk_bound"
    await forward.async_will_remove_from_hass()


async def test_sample_count_without_chunk_bound_is_not_recorded(hass, client):
    entry, ledger = runtime_for(hass, client)
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def final(turn_id, stream):
        async for _ in stream:
            pass
        yield {"type": "final", "text": "server command"}

    client.stt = final
    forward = entity(client, ledger, Fallback())
    await forward.async_process_audio_stream(metadata(), audio([bytes(10)]))
    turn_id = ledger.latest["satellite-a"].turn.turn_id
    await ledger.use_fallback(
        turn_id,
        "stt_fallback",
        "stt.fallback",
        reason="stream_error",
        server_audio_samples=5,
    )
    row = await turn_row(hass, entry, turn_id)
    assert "server_audio_samples" not in row, "count_requires_chunk_bound"
    assert "server_audio_last_chunk_samples" not in row, "bound_requires_count"
    await forward.async_will_remove_from_hass()


async def test_odd_pcm_chunk_is_refused_without_counting_it(hass, client):
    from assist_live_client.protocol import ProtocolError, encode_audio

    entry, ledger = runtime_for(hass, client)
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def encode(turn_id, stream):
        try:
            async for chunk in stream:
                encode_audio(turn_id, 0, chunk)
        except ValueError:
            raise ProtocolError("invalid PCM", kind="malformed") from None
        yield {"type": "final", "text": "unexpected"}

    client.stt = encode
    result = await entity(client, ledger, Fallback()).async_process_audio_stream(
        metadata(), audio([bytes(3)])
    )
    assert result.result == stt.SpeechResultState.SUCCESS, "odd_pcm_uses_fallback"
    turn_id = ledger.latest["satellite-a"].turn.turn_id
    row = await turn_row(hass, entry, turn_id)
    assert row.get("server_audio_samples") == 0, "odd_pcm_is_not_a_sent_sample"
    assert row.get("server_audio_last_chunk_samples") == 0, "odd_pcm_is_not_a_chunk"


@pytest.mark.parametrize(
    "blocked_write,expected,last", [(1, 1, 1), (2, 4, 3), (3, 9, 5)]
)
async def test_pinned_client_write_interval_contains_completed_writes(
    hass, monkeypatch, blocked_write, expected, last
):
    writes = []
    taken = [asyncio.Event() for _ in range(3)]
    blocked = asyncio.Event()

    class Socket:
        turn_id = None

        async def send_json(self, message):
            self.turn_id = message["turn_id"]

        async def send_bytes(self, message):
            _, pcm = decode_audio(message, self.turn_id)
            index = len(writes)
            if index + 1 == blocked_write:
                blocked.set()
                await asyncio.Event().wait()
            writes.append(pcm)
            taken[index].set()

        async def receive(self, **kwargs):
            if not hasattr(self, "negotiated"):
                self.negotiated = True
                return aiohttp.WSMessage(
                    aiohttp.WSMsgType.TEXT,
                    json.dumps(
                        encode_control("negotiated", self.turn_id, capabilities=["stt"])
                    ),
                    "",
                )
            await blocked.wait()
            return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, None, "")

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield Socket()

    async def source():
        for index, size in enumerate((2, 6, 10)):
            yield bytes(size)
            await taken[index].wait()

    async with AssistLiveClient("http://server.invalid", "test-only") as client:
        monkeypatch.setattr(client, "_connect", connect)
        monkeypatch.setattr(
            client,
            "health_capabilities",
            AsyncMock(return_value=HealthCapabilities(True, True, True)),
        )
        monkeypatch.setattr(client, "open_turn", AsyncMock())
        monkeypatch.setattr(client, "event", AsyncMock())
        entry, ledger = runtime_for(hass, client)

        class ReleaseFallback(Fallback):
            async def async_process_audio_stream(self, metadata, stream):
                for event in taken:
                    event.set()
                return await super().async_process_audio_stream(metadata, stream)

        async with asyncio.timeout(2):
            forward = entity(client, ledger, ReleaseFallback())
            result = await forward.async_process_audio_stream(metadata(), source())
        assert result.result == stt.SpeechResultState.SUCCESS
        turn_id = ledger.latest["satellite-a"].turn.turn_id
        row = await turn_row(hass, entry, turn_id)
        assert row.get("server_audio_samples") == expected, (
            "pinned_client_handover_count"
        )
        assert row.get("server_audio_last_chunk_samples") == last, (
            "actual_inflight_chunk"
        )
        written_samples = sum(len(chunk) // 2 for chunk in writes)
        assert expected - last == written_samples, "one_inflight_chunk_bounds_writes"
