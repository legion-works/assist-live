import asyncio
from contextlib import suppress

import pytest
from assist_live_client.models import HealthCapabilities
from assist_live_client.protocol import ProtocolError
from test_stt_forward import Fallback, audio, entity, metadata


@pytest.fixture
async def reader(client, ledger, monkeypatch):
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    active = []

    async def start(ending="result", *, slow_close=False, cancel_before_start=False):
        entered = asyncio.Event()
        release = asyncio.Event()
        closing = asyncio.Event()
        close_gate = asyncio.Event()
        closed = asyncio.Event()
        messages = []

        async def stream(turn_id, source):
            try:
                yield {"type": "final", "text": "synthetic command"}
                entered.set()
                await release.wait()
                if ending in {"cancel", "deadline", "supersede"}:
                    await asyncio.Event().wait()
                elif ending == "protocol":
                    raise ProtocolError("synthetic protocol failure")
                elif ending in {"result", "invalid"}:
                    yield {
                        "type": "speaker_result",
                        "name": "synthetic-speaker",
                        "distance": 0.25 if ending == "result" else 3,
                        "destination": "local",
                        "turn_id": turn_id,
                    }
            finally:
                closing.set()
                try:
                    if slow_close:
                        await close_gate.wait()
                finally:
                    closed.set()

        def source(*args):
            generator = stream(*args)
            messages.append(generator)
            return generator

        client.stt = source
        forward = entity(client, ledger, Fallback())
        active.append((forward, release, close_gate, messages))
        cancelled = []
        create_task = asyncio.create_task

        def create(coroutine, *args, **kwargs):
            task = create_task(coroutine, *args, **kwargs)
            if coroutine.cr_code.co_name in {"_speaker_metadata", "consume"}:
                cancelled.append(task)
                task.cancel()
            return task

        with monkeypatch.context() as context:
            if cancel_before_start:
                context.setattr(asyncio, "create_task", create)
            result = await forward.async_process_audio_stream(
                metadata(), audio([bytes(320)])
            )
        assert result.text == "synthetic command"
        turn_id = ledger.latest["satellite-a"].turn.turn_id
        return {
            "entity": forward,
            "turn_id": turn_id,
            "task": cancelled[0]
            if cancel_before_start
            else ledger.speaker_reads[turn_id].task,
            "messages": messages[0],
            "entered": entered,
            "release": release,
            "closing": closing,
            "close_gate": close_gate,
            "closed": closed,
        }

    yield start
    for forward, release, close_gate, messages in active:
        release.set()
        close_gate.set()
        await forward.async_will_remove_from_hass()
        for generator in messages:
            await generator.aclose()


@pytest.mark.parametrize(
    "ending",
    ["result", "invalid", "exhausted", "protocol", "deadline", "cancel", "supersede"],
)
async def test_every_reader_exit_closes_generator_and_removes_owner(
    reader, ledger, monkeypatch, ending
):
    state = await reader(ending)
    await state["entered"].wait()
    state["release"].set()
    if ending == "deadline":
        clock = asyncio.get_running_loop().time
        monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock() + 11)
    if ending == "cancel":
        await state["entity"].async_will_remove_from_hass()
    elif ending == "supersede":
        await ledger.prepare_speaker_read("satellite-a")
    else:
        await asyncio.wait_for(state["task"], 1)
    assert state["messages"].ag_frame is None
    assert state["closed"].is_set()
    assert ledger.speaker_reads == {}
    assert ledger.speaker_missing == (
        ending in {"invalid", "exhausted", "protocol", "deadline"}
    )


@pytest.mark.parametrize("stopper", ["entity", "ledger"])
async def test_cancel_before_reader_first_step_closes_generator_and_removes_owner(
    reader, ledger, stopper
):
    state = await reader(cancel_before_start=True)
    assert not state["entered"].is_set()
    if stopper == "entity":
        await state["entity"].async_will_remove_from_hass()
    else:
        await ledger.stop_speaker_reads()
    assert ledger.speaker_reads == {}
    assert state["messages"].ag_frame is None
    assert state["closed"].is_set()
    assert ledger.speaker_missing == 0
    assert ledger.speaker_superseded == 0


async def test_received_reader_is_not_pending_during_generator_close(reader, ledger):
    state = await reader(slow_close=True)
    state["release"].set()
    await state["closing"].wait()
    assert ledger.speaker_reads == {}
    await ledger.prepare_speaker_read("satellite-a")
    assert ledger.speaker_superseded == 0


async def test_cancel_during_generator_close_leaves_no_owner(reader, ledger):
    state = await reader(slow_close=True)
    state["release"].set()
    await state["closing"].wait()
    await state["entity"].async_will_remove_from_hass()
    assert ledger.speaker_reads == {}
    assert state["messages"].ag_frame is None
    assert ledger.speaker_superseded == 0


async def test_superseding_during_received_close_does_not_count_superseded(
    reader, ledger
):
    state = await reader(slow_close=True)
    state["release"].set()
    await state["closing"].wait()
    await ledger.prepare_speaker_read("satellite-a")
    assert ledger.speaker_superseded == 0
    assert ledger.speaker_reads == {}
    with suppress(asyncio.CancelledError):
        state["task"].cancel()
        await state["task"]
