import asyncio
from contextlib import suppress
from dataclasses import replace

import pytest
from assist_live_client.models import HealthCapabilities
from assist_live_client.protocol import ProtocolError
from homeassistant.components import stt
from test_stt_forward import Fallback, audio, entity, metadata


class Clock:
    def __init__(self, monkeypatch):
        loop = asyncio.get_running_loop()
        self.now = loop.time()
        monkeypatch.setattr(loop, "time", lambda: self.now)
        monkeypatch.setattr(loop, "slow_callback_duration", float("inf"))

    async def move(self, seconds):
        self.now += seconds
        for _ in range(4):
            loop = asyncio.get_running_loop()
            checkpoint = loop.create_future()
            loop.call_later(0, checkpoint.set_result, None)
            await checkpoint


@pytest.fixture
async def clock(monkeypatch):
    return Clock(monkeypatch)


def stages(ledger):
    return next(iter(ledger.records.values())).stages


@pytest.mark.parametrize("delay,expected", [(0.199, "server"), (0.201, "test command")])
async def test_health_has_its_own_deadline(client, ledger, clock, delay, expected):
    entered, release = asyncio.Event(), asyncio.Event()

    async def health(**kwargs):
        entered.set()
        await release.wait()
        return HealthCapabilities(True, True, False)

    async def final(*args):
        yield {"type": "final", "text": "server"}

    client.health_capabilities.side_effect = health
    client.stt = final
    fallback = Fallback()
    forward = entity(client, ledger, fallback)
    task = asyncio.create_task(
        forward.async_process_audio_stream(metadata(), audio([bytes(320)]))
    )
    await entered.wait()
    await clock.move(delay)
    if delay > 0.2:
        assert task.done()
    release.set()
    result = await task
    assert result.text == expected
    assert forward.audio_processing.requires_external_vad is False


@pytest.mark.parametrize("error", [ProtocolError, TimeoutError, OSError])
async def test_health_exception_selects_local_path(client, ledger, error):
    client.health_capabilities.side_effect = error("unavailable")
    result = await entity(client, ledger, Fallback()).async_process_audio_stream(
        metadata(), audio([bytes(320)])
    )
    assert result.text == "test command"
    assert "fallback_used" in stages(ledger)


async def test_slow_ledger_save_does_not_use_health_budget(client, ledger, clock):
    saving, saved = asyncio.Event(), asyncio.Event()

    async def save():
        if saved.is_set():
            return
        saving.set()
        await saved.wait()

    async def health(**kwargs):
        await clock.move(0)
        return HealthCapabilities(True, True, False)

    async def final(*args):
        yield {"type": "final", "text": "server after slow save"}

    ledger.save = save
    client.health_capabilities.side_effect = health
    client.stt = final
    task = asyncio.create_task(
        entity(client, ledger, Fallback()).async_process_audio_stream(
            metadata(), audio([bytes(320)])
        )
    )
    await saving.wait()
    await clock.move(0.3)
    saved.set()
    result = await task
    assert result.text == "server after slow save"
    assert "fallback_used" not in stages(ledger)


@pytest.mark.parametrize("voiced", [False, True])
async def test_only_server_messages_reset_receive_timer(client, ledger, clock, voiced):
    incoming = asyncio.Queue()
    sending, receiving = asyncio.Event(), asyncio.Event()
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def source():
        while True:
            yield (b"\x01\x00" if voiced else b"\x00\x00") * 160
            sending.set()
            await incoming.get()

    async def stalled(turn_id, stream):
        await anext(stream)
        yield {"type": "heartbeat"}
        receiving.set()
        await asyncio.Event().wait()

    client.stt = stalled
    fallback = Fallback()
    task = asyncio.create_task(
        entity(client, ledger, fallback).async_process_audio_stream(
            metadata(), source()
        )
    )
    await receiving.wait()
    await clock.move(0.599)
    assert "fallback_used" not in stages(ledger)
    sending.clear()
    incoming.put_nowait(None)
    await sending.wait()
    await clock.move(0.002)
    assert "fallback_used" in stages(ledger)
    await clock.move(14.400)
    result = await task
    assert result.text == "test command"


async def test_heartbeat_extends_receive_deadline(client, ledger, clock):
    messages = asyncio.Queue()
    entered = asyncio.Event()
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def server(*args):
        while True:
            entered.set()
            yield await messages.get()

    client.stt = server
    fallback = Fallback()
    task = asyncio.create_task(
        entity(client, ledger, fallback).async_process_audio_stream(
            metadata(), audio([bytes(320)])
        )
    )
    await entered.wait()
    await clock.move(0.599)
    messages.put_nowait({"type": "heartbeat"})
    await clock.move(0)
    await clock.move(0.599)
    assert "fallback_used" not in stages(ledger)
    messages.put_nowait({"type": "final", "text": "complete"})
    assert (await task).text == "complete"
    assert not hasattr(fallback, "audio")


@pytest.mark.parametrize("error", [ProtocolError, OSError, TimeoutError])
async def test_final_result_prevents_fallback_even_on_close_error(
    client, ledger, error
):
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def final(*args):
        try:
            yield {"type": "final", "text": "complete command"}
        finally:
            raise error("late close")

    client.stt = final
    fallback = Fallback()
    result = await entity(client, ledger, fallback).async_process_audio_stream(
        metadata(), audio([bytes(320)])
    )
    assert result.text == "complete command"
    assert not hasattr(fallback, "audio")
    assert stages(ledger) >= {"stt_final"}
    assert "fallback_used" not in stages(ledger)


async def test_partial_then_disconnect_replays_kept_and_live_once(client, ledger):
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    release = asyncio.Event()
    chunks = [bytes([i, 0]) * 160 for i in (1, 2, 3)]

    async def source():
        yield chunks[0]
        await release.wait()
        yield chunks[1]
        yield chunks[2]

    async def lost(turn_id, stream):
        assert await anext(stream) == chunks[0]
        yield {"type": "partial", "text": "unsafe partial"}
        raise ProtocolError("closed")

    class Local(Fallback):
        async def async_process_audio_stream(self, metadata, stream):
            self.audio = await anext(stream)
            release.set()
            self.audio += b"".join([chunk async for chunk in stream])
            return stt.SpeechResult("local command", stt.SpeechResultState.SUCCESS)

    client.stt = lost
    fallback = Local()
    result = await entity(client, ledger, fallback).async_process_audio_stream(
        metadata(), source()
    )
    assert result.text == "local command"
    assert fallback.audio == b"".join(chunks)
    assert "fallback_used" in stages(ledger)
    events = [call.args[0] for call in client.event.call_args_list]
    assert [event.stage for event in events] == [
        "fallback_used",
        "stt_final",
    ]
    assert all(event.source == "integration" for event in events)


async def test_local_vad_uses_voice_from_before_disconnect(client, ledger, monkeypatch):
    class Vad:
        def Process10ms(self, chunk):
            return 1.0 if chunk[0] else 0.0

    monkeypatch.setattr("custom_components.assist_live.stt.MicroVad", Vad)
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def lost(turn_id, stream):
        await anext(stream)
        raise ProtocolError("closed")
        yield

    client.stt = lost
    fallback = Fallback()
    await entity(client, ledger, fallback).async_process_audio_stream(
        metadata(), audio([b"\x01\x00" * 160] * 30 + [bytes(320)] * 100)
    )
    assert len(fallback.audio) == 32000


@pytest.mark.parametrize("health", [True, False])
async def test_fallback_deadline_is_from_turn_start(client, ledger, clock, health):
    entered = asyncio.Event()
    client.health_capabilities.return_value = HealthCapabilities(health, health, health)

    async def late_failure(*args):
        yield {"type": "heartbeat"}
        await asyncio.Event().wait()

    class Blocked(Fallback):
        calls = 0

        async def async_process_audio_stream(self, metadata, stream):
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()

    client.stt = late_failure
    fallback = Blocked()
    task = asyncio.create_task(
        entity(client, ledger, fallback).async_process_audio_stream(
            metadata(), audio([bytes(320)])
        )
    )
    await clock.move(0)
    if health:
        await clock.move(0.601)
    await entered.wait()
    await clock.move(19.398 if health else 19.999)
    assert not task.done()
    await clock.move(0.002)
    assert task.done()
    result = await task
    assert not result.text
    assert result.result == stt.SpeechResultState.ERROR
    assert fallback.calls == 1
    assert "stt_failed" in stages(ledger)


async def test_audio_stops_at_initial_fifteen_second_deadline(clock):
    from custom_components.assist_live.stt_fallback import FallbackAudio

    incoming, entered = asyncio.Queue(), asyncio.Event()
    cancelled = asyncio.Event()

    async def source():
        entered.set()
        while True:
            yield await incoming.get()

    turn_audio = FallbackAudio(source(), started=clock.now, cancelled=cancelled)
    reader = aiter(turn_audio.server_audio())
    try:
        await entered.wait()
        await clock.move(14.999)
        incoming.put_nowait(b"before")
        assert await anext(reader) == b"before"
        await clock.move(0.002)
        incoming.put_nowait(b"after")

        async def remaining():
            async with asyncio.timeout(1):
                return [chunk async for chunk in reader]

        pending = asyncio.create_task(remaining())
        await clock.move(0)
        await clock.move(1.001)
        assert await pending == []
        assert (
            b"".join([chunk async for chunk in turn_audio.server_audio()]) == b"before"
        )
    finally:
        await turn_audio.aclose()
    assert cancelled.is_set()


async def test_kept_audio_is_bounded_and_replayed_in_order(clock):
    from custom_components.assist_live.stt_fallback import FallbackAudio

    turn_audio = FallbackAudio(
        audio([b"a" * 479999, b"bc", b"d"]),
        started=clock.now,
        cancelled=asyncio.Event(),
    )
    try:
        first = b"".join([chunk async for chunk in turn_audio.server_audio()])
        replay = b"".join([chunk async for chunk in turn_audio.server_audio()])
        assert first == b"a" * 479999 + b"b"
        assert replay == first
    finally:
        await turn_audio.aclose()


async def test_failed_fallback_never_returns_partial_text(client, ledger):
    class Failed(Fallback):
        async def async_process_audio_stream(self, metadata, stream):
            return stt.SpeechResult("unsafe partial", stt.SpeechResultState.ERROR)

    result = await entity(client, ledger, Failed()).async_process_audio_stream(
        metadata(), audio([bytes(320)])
    )
    assert not result.text
    assert result.result == stt.SpeechResultState.ERROR
    assert "stt_failed" in stages(ledger)


@pytest.mark.parametrize(
    "capability",
    [
        "supported_languages",
        "supported_formats",
        "supported_codecs",
        "supported_bit_rates",
        "supported_sample_rates",
        "supported_channels",
    ],
)
async def test_compatibility_checked_again_at_failover(client, ledger, capability):
    fallback = Fallback()
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def lost(*args):
        setattr(fallback, capability, [])
        raise ProtocolError("closed")
        yield

    client.stt = lost
    result = await entity(client, ledger, fallback).async_process_audio_stream(
        metadata(), audio([bytes(320)])
    )
    assert not result.text
    assert result.result == stt.SpeechResultState.ERROR
    assert not hasattr(fallback, "audio")
    assert "stt_failed" in stages(ledger)


@pytest.mark.parametrize("through_wrapper", [False, True])
async def test_recursive_fallback_is_refused(client, ledger, through_wrapper):
    fallback = Fallback()
    forward = entity(client, ledger, fallback)
    if through_wrapper:
        fallback.fallback = forward
    else:
        forward.fallback = forward
    result = await forward.async_process_audio_stream(metadata(), audio([bytes(320)]))
    assert not result.text
    assert result.result == stt.SpeechResultState.ERROR
    assert not hasattr(fallback, "audio")
    client.health_capabilities.assert_not_awaited()


async def test_cancellation_stops_the_live_source(client, ledger):
    entered, closed = asyncio.Event(), asyncio.Event()

    async def source():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield b"unused"
        finally:
            closed.set()

    task = asyncio.create_task(
        entity(client, ledger, Fallback()).async_process_audio_stream(
            metadata(), source()
        )
    )
    await entered.wait()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_incompatible_audio_is_refused_before_consumption(client, ledger):
    fallback = Fallback()
    result = await entity(client, ledger, fallback).async_process_audio_stream(
        replace(metadata(), language="zz"), audio([bytes(320)])
    )
    assert not result.text
    assert not hasattr(fallback, "audio")


async def test_recursive_selection_introduced_during_turn_is_refused(client, ledger):
    fallback = Fallback()
    forward = entity(client, ledger, fallback)
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def lost(*args):
        fallback.fallback = forward
        raise ProtocolError("closed")
        yield

    client.stt = lost
    result = await forward.async_process_audio_stream(metadata(), audio([bytes(320)]))
    assert not result.text
    assert result.result == stt.SpeechResultState.ERROR
    assert not hasattr(fallback, "audio")


@pytest.mark.parametrize("source_error", [True, False])
async def test_unexpected_failure_is_empty_without_retry(
    client, ledger, source_error, caplog
):
    import logging

    secret = "planted-sensitive-stt-content"

    class Failed(Fallback):
        calls = 0

        async def async_process_audio_stream(self, metadata, stream):
            self.calls += 1
            if source_error:
                async for _ in stream:
                    pass
            raise ValueError(secret)

    async def broken_source():
        yield bytes(320)
        raise ValueError(secret)

    fallback = Failed()
    result = await entity(client, ledger, fallback).async_process_audio_stream(
        metadata(), broken_source()
    )
    assert not result.text
    assert result.result == stt.SpeechResultState.ERROR
    assert fallback.calls == 1
    assert "stt_failed" in stages(ledger)
    records = [
        record
        for record in caplog.records
        if record.name == "custom_components.assist_live.stt"
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "ValueError" in records[0].getMessage()
    assert records[0].exc_info is None
    assert secret not in caplog.text
