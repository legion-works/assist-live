import asyncio
from contextlib import aclosing, asynccontextmanager

import pytest
from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd
from assist_live_client.protocol import ProtocolError
from homeassistant.components.tts import TTSAudioRequest
from pytest_homeassistant_custom_component.common import async_capture_events
from test_stt_fallback import clock  # noqa: F401
from test_tts_early_start import PCM, SILENCE, WAV_22050, HeldServer, start
from test_tts_fallback import StreamingFallback
from test_tts_forward import Fallback, entity
from test_tts_stream import capable

from custom_components.assist_live import tts


@pytest.fixture
def recovery_waits(monkeypatch):
    waits = asyncio.Queue()

    class RecoverySchedule:
        condition = None

        def __getattr__(self, name):
            return getattr(asyncio, name)

        def Condition(self):
            self.condition = asyncio.Condition()
            return self.condition

        @asynccontextmanager
        async def timeout(self, seconds):
            async with asyncio.timeout(seconds) as timer:
                if self.condition is not None:
                    waits.put_nowait((self.condition, timer))
                yield

    monkeypatch.setattr(tts, "asyncio", RecoverySchedule())
    return waits


async def test_early_start_at_production_deadline(client, ledger, clock):  # noqa: F811
    server = HeldServer(client)
    task = asyncio.create_task(start(entity(client, ledger, Fallback())))
    try:
        await server.waiting.wait()
        await clock.move(0.999)
        assert not task.done()

        await clock.move(0.002)
        assert task.done(), "WAV output must be available after one second"
        response = task.result()
        async with aclosing(response.data_gen) as output:
            assert response.extension == "wav"
            assert await anext(output) == WAV_22050
            assert await anext(output) == SILENCE
            server.release.set()
            assert [chunk async for chunk in output] == [PCM]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def broken_reply(client, ledger, hass, parts):
    capable(client)
    first = "First sentence. "

    async def source():
        yield first
        async for part in parts:
            yield part

    async def server(turn, text, voice, *, text_positions):
        assert await anext(text) == first
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(PCM)
        yield TTSSentenceEnd(0, len(first.rstrip()))
        raise ProtocolError("server closed")

    client.tts_stream = server
    fallback = StreamingFallback()
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    response = await forward.async_stream_tts_audio(TTSAudioRequest("en", {}, source()))

    async def consume():
        return b"".join([chunk async for chunk in response.data_gen])

    return consume(), fallback


async def test_finish_text_expires_at_production_idle_limit(
    client,
    ledger,
    hass,
    clock,  # noqa: F811
    recovery_waits,
):
    release = asyncio.Event()

    async def parts():
        await release.wait()
        yield "Too late."

    events = async_capture_events(hass, "assist_live_tts_failed")
    consume, fallback = await broken_reply(client, ledger, hass, parts())
    async with asyncio.TaskGroup() as tasks:
        output = tasks.create_task(consume)
        _, timer = await recovery_waits.get()
        await clock.move(1.99)
        assert not timer.expired() and not output.done()
        await clock.move(0.02)
        release.set()

    await hass.async_block_till_done()
    assert [event.data["text"] for event in events] == ["First sentence. "]
    assert output.result()[44:] == PCM
    assert fallback.received_text == []


async def test_finish_text_rechecks_progress_at_timeout(
    client, ledger, hass, recovery_waits
):
    release_next = asyncio.Event()
    next_ready = asyncio.Event()
    release_end = asyncio.Event()

    async def parts():
        await release_next.wait()
        next_ready.set()
        yield "Second sentence. "
        await release_end.wait()
        yield "Third sentence."

    events = async_capture_events(hass, "assist_live_tts_failed")
    consume, fallback = await broken_reply(client, ledger, hass, parts())
    async with asyncio.TaskGroup() as tasks:
        output = tasks.create_task(consume)
        condition, timer = await recovery_waits.get()
        loop = asyncio.get_running_loop()
        async with condition:
            # Queue the text producer before the waiter tries to get the lock again.
            release_next.set()
            await next_ready.wait()
            timer.reschedule(loop.time() - 1)
            checkpoint = loop.create_future()
            loop.call_soon(checkpoint.set_result, None)
            await checkpoint
            assert output.cancelling()
        # Give recovery the lock before the producer can finish the text.
        async with condition:
            release_end.set()

    await hass.async_block_till_done()
    assert fallback.received_text == ["Second sentence. Third sentence."]
    assert output.result()[44:] == PCM + fallback.audio[44:]
    assert events == []
