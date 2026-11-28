import asyncio
from contextlib import aclosing

from assist_live_client.models import HealthCapabilities
from assist_live_client.protocol import ProtocolError
from homeassistant.components import stt
from test_stt_fallback import clock  # noqa: F401
from test_stt_forward import Fallback, audio, entity, metadata

from custom_components.assist_live.stt_fallback import FallbackAudio


async def test_stt_health_passes_budget_to_client(client, ledger, clock):  # noqa: F811
    checking = asyncio.Event()
    probe_exited = asyncio.Event()

    async def health(*, timeout):
        checking.set()
        try:
            await asyncio.Future()
        finally:
            probe_exited.set()

    client.health_capabilities.side_effect = health
    forward = entity(client, ledger, Fallback())
    task = asyncio.create_task(
        forward.async_process_audio_stream(metadata(), audio([bytes(320)]))
    )
    try:
        await checking.wait()
        await clock.move(0.199)
        assert not probe_exited.is_set()
        await clock.move(0.002)
        assert probe_exited.is_set()
        assert (await task).result == stt.SpeechResultState.SUCCESS
        client.health_capabilities.assert_awaited_once_with(timeout=0.2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_local_vad_equal_half_is_not_speech():
    class Vad:
        def Process10ms(self, frame):
            return 0.5 if frame[0] == 1 else 0.0

    half, quiet, marker = b"\x01\x00" * 160, bytes(320), b"\x02\x00" * 160
    frames = [half, *([quiet] * 70), marker]
    buffered = FallbackAudio(
        audio(frames),
        started=asyncio.get_running_loop().time(),
        cancelled=asyncio.Event(),
    )
    async with aclosing(buffered):
        async with aclosing(buffered.local_audio(Vad())) as stream:
            assert [frame async for frame in stream] == frames


async def test_fallback_closes_source_at_buffer_cap():
    closed = asyncio.Event()
    limit = bytes(480000)

    async def upstream():
        try:
            yield limit
            raise AssertionError("read beyond the buffer limit")
        finally:
            closed.set()

    async with aclosing(upstream()) as source:
        buffered = FallbackAudio(
            source,
            started=asyncio.get_running_loop().time(),
            cancelled=asyncio.Event(),
        )
        async with aclosing(buffered):
            assert b"".join([chunk async for chunk in buffered.server_audio()]) == limit
            assert not closed.is_set()
        assert closed.is_set(), "closing the buffer must close the upstream generator"


async def test_failed_fallback_text_is_not_recorded(
    client, ledger, drain_timing_events
):
    class FailedFallback(Fallback):
        async def async_process_audio_stream(self, metadata, stream):
            await super().async_process_audio_stream(metadata, stream)
            return stt.SpeechResult("partial text", stt.SpeechResultState.ERROR)

    ledger.record_transcripts = True
    client.health_capabilities.return_value = HealthCapabilities(True, True, False)

    async def lost(turn_id, stream):
        await anext(stream)
        raise ProtocolError("server closed")
        yield

    client.stt = lost
    result = await entity(client, ledger, FailedFallback()).async_process_audio_stream(
        metadata(), audio([bytes(320)])
    )
    await drain_timing_events(ledger)

    assert result == stt.SpeechResult("", stt.SpeechResultState.ERROR)
    assert client.open_turn.call_args.args[0].record_transcripts is True
    events = [call.args[0] for call in client.event.await_args_list]
    failed = [event for event in events if event.stage == "stt_failed"]
    assert len(failed) == 1
    assert failed[0].data == {}
    assert ledger.failures["stt_failed"] == 1
