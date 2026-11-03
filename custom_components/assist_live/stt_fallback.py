import asyncio
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from contextlib import suppress

from assist_live_client.protocol import ProtocolError
from pymicro_vad import MicroVad


class FallbackAudio:
    """Keep one bounded turn independently of each audio consumer."""

    def __init__(
        self,
        stream: AsyncIterable[bytes],
        *,
        started: float,
        cancelled: asyncio.Event,
    ) -> None:
        self.started = started
        self.cancelled = cancelled
        self._source = aiter(stream)
        self._kept = bytearray()
        self._changed = asyncio.Condition()
        self._done = False
        self._error: Exception | None = None
        self.server_audio_samples = 0
        self.server_audio_last_chunk_samples = 0
        self._producer = asyncio.create_task(self._collect())

    async def _collect(self) -> None:
        try:
            async with asyncio.timeout_at(self.started + 15):
                async for chunk in self._source:
                    if self.cancelled.is_set():
                        break
                    async with self._changed:
                        self._kept.extend(chunk[: 480000 - len(self._kept)])
                        self._changed.notify_all()
                    if len(self._kept) >= 480000:
                        break
        except TimeoutError:
            pass
        except Exception as err:
            self._error = err
        finally:
            async with self._changed:
                self._done = True
                self._changed.notify_all()

    async def server_audio(self) -> AsyncIterator[bytes]:
        async for chunk in self._audio():
            if len(chunk) % 2:
                raise ProtocolError("invalid PCM length", kind="malformed")
            # The client awaits each write before it requests the next chunk.
            # The last chunk can be in flight, not a completed socket write.
            self.server_audio_last_chunk_samples = len(chunk) // 2
            self.server_audio_samples += self.server_audio_last_chunk_samples
            yield chunk

    async def _audio(self) -> AsyncIterator[bytes]:
        offset = 0

        def available() -> bool:
            return len(self._kept) > offset or self._done

        while not self.cancelled.is_set():
            async with self._changed:
                await self._changed.wait_for(available)
                if len(self._kept) == offset:
                    if self._error is not None:
                        raise self._error
                    return
                chunk = bytes(self._kept[offset : offset + 32000])
                offset += len(chunk)
            yield chunk

    async def local_audio(self, vad: MicroVad) -> AsyncIterator[bytes]:
        buffer = bytearray()
        spoken = False
        quiet = 0
        count = 0
        # Replay also supplies the voice history for the local end decision.
        async for chunk in self._audio():
            buffer.extend(chunk)
            while len(buffer) >= 320:
                frame = bytes(buffer[:320])
                del buffer[:320]
                if vad.Process10ms(frame) > 0.5:
                    spoken = True
                    quiet = 0
                elif spoken:
                    quiet += 1
                count += 1
                yield frame
                if quiet >= 70 or count >= 1500:
                    return
        if buffer and count < 1500:
            yield bytes(buffer)

    async def aclose(self) -> None:
        self.cancelled.set()
        self._producer.cancel()
        with suppress(asyncio.CancelledError):
            await self._producer
        if isinstance(self._source, AsyncGenerator):
            await self._source.aclose()
