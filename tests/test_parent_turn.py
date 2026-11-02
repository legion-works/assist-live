import asyncio
import logging

import pytest
from assist_live_client.models import HealthCapabilities
from homeassistant.components.tts import TTSAudioRequest
from homeassistant.exceptions import HomeAssistantError
from test_stt_forward import Fallback, audio, entity, metadata
from test_tts_forward import Fallback as TTSFallback
from test_tts_forward import entity as tts_entity
from test_tts_forward import text

from custom_components.assist_live.ledger import TurnLedger


async def test_fallback_chain_recovers_without_absent_parent(client):
    now = [0.0]
    ledger = TurnLedger(client, clock=lambda: now[0])
    forward = entity(client, ledger, Fallback())

    async def accepted(turn):
        assert turn.parent_turn_id is None

    async def final(turn_id, stream):
        yield {"type": "final", "text": "server command"}

    client.open_turn.side_effect = accepted
    client.stt = final
    for _ in range(3):
        client.health_capabilities.return_value = HealthCapabilities(
            False, False, False
        )
        result = await forward.async_process_audio_stream(
            metadata(), audio([bytes(320)])
        )
        assert result.text == "test command"
        fallback_turn = ledger.current("satellite-a")
        ledger.device_event("satellite-a", fallback_turn.turn_id, "playback_end")
        now[0] += 0.1
        client.health_capabilities.return_value = HealthCapabilities(True, True, True)
        result = await forward.async_process_audio_stream(
            metadata(), audio([bytes(320)])
        )
        assert result.text == "server command"
        now[0] += 0.1
    assert client.open_turn.await_count == 3
    await forward.async_will_remove_from_hass()


@pytest.mark.parametrize("gap,parent", [(9.9, True), (10.0, False), (10.1, False)])
@pytest.mark.parametrize("kind", ["voice", "tts_only"])
async def test_parent_requires_recent_server_playback_end(client, gap, parent, kind):
    now = [0.0]
    ledger = TurnLedger(client, clock=lambda: now[0])
    turn = (
        ledger.open_voice("satellite-a")
        if kind == "voice"
        else ledger.open_tts("satellite-a")
    )
    assert await ledger.connect(turn)
    now[0] = 1.0
    ledger.device_event("satellite-a", turn.turn_id, "playback_end")
    now[0] += gap
    answer = ledger.open_voice("satellite-a")
    assert answer.parent_turn_id == (turn.turn_id if parent else None)


@pytest.mark.parametrize("closure", ["superseded", "limit", "restart"])
async def test_administrative_close_never_becomes_parent(client, closure):
    now = [0.0]
    ledger = TurnLedger(client, clock=lambda: now[0])
    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    now[0] = 0.1
    ledger.mark(turn.turn_id, "stt_final")
    if closure == "limit":
        now[0] = 120.0
    elif closure == "restart":
        saved = ledger.snapshot()
        ledger = TurnLedger(client, clock=lambda: now[0])
        await ledger.restore(saved)
    else:
        now[0] = 0.2
    answer = ledger.open_voice("satellite-a")
    assert answer.parent_turn_id is None
    if closure != "restart" and turn.turn_id in ledger.records:
        assert ledger.records[turn.turn_id].closed == 0.1


async def test_terminal_playback_end_is_required_even_for_server_turn(client):
    now = [0.0]
    ledger = TurnLedger(client, clock=lambda: now[0])
    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    now[0] = 0.1
    ledger.mark(turn.turn_id, "stt_final")
    assert ledger.open_voice("satellite-a").parent_turn_id is None


@pytest.mark.parametrize("pruned", [False, True])
async def test_superseded_during_open_does_not_start_fallback(client, caplog, pruned):
    now = [0.0]
    ledger = TurnLedger(client, clock=lambda: now[0])
    fallback = Fallback()
    forward = entity(client, ledger, fallback)
    entered, release = asyncio.Event(), asyncio.Event()
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def blocked(turn):
        entered.set()
        await release.wait()

    audio_started = []

    async def empty_stream(*args):
        audio_started.append(True)
        return
        yield

    client.open_turn.side_effect = blocked
    client.stt = empty_stream
    with caplog.at_level(logging.DEBUG, logger="custom_components.assist_live"):
        task = asyncio.create_task(
            forward.async_process_audio_stream(metadata(), audio([bytes(320)]))
        )
        await asyncio.wait_for(entered.wait(), 1)
        old_turn = ledger.current("satellite-a")
        now[0] = 0.1
        replacement = ledger.open_voice("satellite-a")
        if pruned:
            now[0] = 11.0
            ledger.prune()
        release.set()
        result = await asyncio.wait_for(task, 1)
    assert result.text == ""
    assert not hasattr(fallback, "audio")
    assert ledger.current("satellite-a") == replacement
    assert not any(
        "fallback_used" in record.stages for record in ledger.records.values()
    )
    assert not any(
        call.args[0].stage == "fallback_used" for call in client.event.call_args_list
    )
    assert old_turn.turn_id not in ledger._pending_opens
    await ledger.flush()
    assert (
        sum(
            call.args[0].stage == "turn_superseded"
            and call.args[0].turn_id == old_turn.turn_id
            for call in client.event.call_args_list
        )
        == 1
    )
    assert not audio_started
    assert client.open_turn.await_count == 1
    now[0] = 12
    ledger.prune()
    assert old_turn.turn_id not in ledger._connected
    assert "turn superseded during open" in caplog.text
    assert "KeyError" not in caplog.text


@pytest.mark.parametrize("pruned", [False, True])
async def test_superseded_tts_open_does_not_play_fallback(client, caplog, pruned):
    now = [0.0]
    ledger = TurnLedger(client, clock=lambda: now[0])
    fallback = TTSFallback()
    forward = tts_entity(client, ledger, fallback)
    entered, release = asyncio.Event(), asyncio.Event()
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def blocked(turn):
        entered.set()
        await release.wait()

    audio_started = []

    async def empty_stream(*args, **kwargs):
        audio_started.append(True)
        return
        yield

    client.open_turn.side_effect = blocked
    client.tts_stream = empty_stream
    with caplog.at_level(logging.DEBUG, logger="custom_components.assist_live"):
        task = asyncio.create_task(
            forward.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
        )
        await asyncio.wait_for(entered.wait(), 1)
        old_turn = ledger.current("satellite-a")
        now[0] = 0.1
        replacement = ledger.open_voice("satellite-a")
        if pruned:
            now[0] = 11.0
            ledger.prune()
        release.set()
        with pytest.raises(HomeAssistantError, match="turn superseded during open"):
            await asyncio.wait_for(task, 1)
    assert ledger.current("satellite-a") == replacement
    assert not hasattr(fallback, "text")
    assert not any(
        "fallback_used" in record.stages for record in ledger.records.values()
    )
    await ledger.flush()
    assert (
        sum(
            call.args[0].stage == "turn_superseded"
            and call.args[0].turn_id == old_turn.turn_id
            for call in client.event.call_args_list
        )
        == 1
    )
    assert not audio_started
    assert client.open_turn.await_count == 1
    assert old_turn.turn_id not in ledger._pending_opens
    now[0] = 12
    ledger.prune()
    assert old_turn.turn_id not in ledger._connected
    assert "turn superseded during open" in caplog.text
    assert "KeyError" not in caplog.text
