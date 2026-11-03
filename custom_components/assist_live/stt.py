import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from contextlib import suppress
from typing import Any

from assist_live_client import AssistLiveClient
from assist_live_client.protocol import ProtocolError
from homeassistant.components import stt
from homeassistant.components.stt.models import SpeechAudioProcessing
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pymicro_vad import MicroVad

from .const import (
    DOMAIN,
    MAX_SPEAKER_DISTANCE,
    MESSAGE_DEADLINE,
    SPEAKER_RESULT_DEADLINE,
)
from .ledger import FallbackReason, SpeakerRead, TurnLedger, stream_failure_reason
from .repairs import fallback_issue
from .settings import FallbackMetadata, effective_settings
from .stt_fallback import FallbackAudio

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    fallback_id = effective_settings(entry)["fallback_stt"]
    cached = runtime.fallback_metadata.setdefault(
        "stt", {"entity_id": fallback_id, "languages": []}
    )
    entities = []
    for device, satellite in entry.data["satellites"].items():
        entity = AssistLiveSTT(
            satellite,
            device,
            runtime.client,
            runtime.ledger,
            fallback_id,
            entry.entry_id,
            cached,
        )
        if registered := dr.async_get(hass).async_get(device):
            entity._attr_device_info = dr.DeviceInfo(
                identifiers=registered.identifiers,
                connections=registered.connections
                if isinstance(registered, dr.DeviceEntry)
                else set(),
            )
        entities.append(entity)
        runtime.stt[device] = entity
    async_add_entities(entities)


class AssistLiveSTT(stt.SpeechToTextEntity):
    def __init__(
        self,
        satellite: str,
        device: str,
        client: AssistLiveClient,
        ledger: TurnLedger,
        fallback_entity_id: str,
        entry_id: str,
        fallback_metadata: FallbackMetadata | None = None,
    ) -> None:
        self.satellite = satellite
        self.client = client
        self.ledger = ledger
        self.fallback_entity_id = fallback_entity_id
        self.entry_id = entry_id
        self._fallback_metadata: FallbackMetadata = (
            fallback_metadata
            if fallback_metadata is not None
            else {"entity_id": fallback_entity_id, "languages": []}
        )
        self._attr_unique_id = f"{satellite}_stt"
        self._attr_name = f"assist-live {satellite}"
        self._attr_device_info = {"identifiers": {(DOMAIN, device)}}
        self._speaker_tasks: set[asyncio.Task[None]] = set()
        self._speaker_cleanup_tasks: set[asyncio.Task[None]] = set()

    async def async_will_remove_from_hass(self) -> None:
        tasks = list(self._speaker_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            # Task completion does not mean that its cleanup callback ran.
            await asyncio.wait(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*self._speaker_cleanup_tasks, return_exceptions=True)

    async def _close_speaker_messages(
        self, messages: AsyncIterator[dict[str, Any]]
    ) -> None:
        if isinstance(messages, AsyncGenerator):
            with suppress(Exception):
                async with asyncio.timeout(MESSAGE_DEADLINE):
                    await messages.aclose()

    def _start_speaker_read(
        self, messages: AsyncIterator[dict[str, Any]], turn_id: str, deadline: float
    ) -> None:
        started = False

        async def consume() -> None:
            nonlocal started
            started = True
            await self._speaker_metadata(messages, turn_id, deadline)

        task = asyncio.create_task(consume())
        read = SpeakerRead(self.satellite, task)
        self.ledger.speaker_reads[turn_id] = read
        self._speaker_tasks.add(task)

        def finished(task: asyncio.Task[None]) -> None:
            self.ledger.speaker_reads.pop(turn_id, None)
            self._speaker_tasks.discard(task)
            if not started:
                # Pre-start cancellation cannot enter the reader's finally.
                # Keep its close task so unload waits for socket cleanup.
                read.cleanup = asyncio.create_task(
                    self._close_speaker_messages(messages)
                )
                self._speaker_cleanup_tasks.add(read.cleanup)
                read.cleanup.add_done_callback(self._speaker_cleanup_tasks.discard)

        task.add_done_callback(finished)

    async def _speaker_metadata(
        self, messages: AsyncIterator[dict[str, Any]], turn_id: str, deadline: float
    ) -> None:
        received = False
        cancelled = False
        try:
            async with asyncio.timeout_at(deadline):
                async for message in messages:
                    if message["type"] == "speaker_result":
                        if not 0 <= message["distance"] <= MAX_SPEAKER_DISTANCE:
                            break
                        self.ledger.speaker_result(
                            turn_id, message, satellite=self.satellite
                        )
                        received = True
                        break
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            pass
        finally:
            self.ledger.speaker_reads.pop(turn_id, None)
            if (
                not received
                and not cancelled
                and not self.ledger.has_speaker_result(turn_id)
            ):
                self.ledger.speaker_missing += 1
            await self._close_speaker_messages(messages)

    @property
    def supported_languages(self) -> list[str]:
        self.resolve_fallback()
        return self._fallback_metadata["languages"]

    def resolve_fallback(self) -> stt.SpeechToTextEntity | None:
        try:
            engine = stt.async_get_speech_to_text_entity(
                self.hass, self.fallback_entity_id
            )
        except KeyError:
            engine = None
        if engine is not None and not engine.available:
            engine = None
        if engine is not None and not isinstance(engine, AssistLiveSTT):
            self._fallback_metadata["languages"] = list(engine.supported_languages)
        if engine is not None:
            fallback_issue(
                self.hass, self.entry_id, "stt", self.fallback_entity_id, False
            )
        return engine

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        return [stt.AudioChannels.CHANNEL_MONO]

    @property
    def audio_processing(self) -> SpeechAudioProcessing:
        return SpeechAudioProcessing(False, True, True)

    def _fallback_compatible(
        self, metadata: stt.SpeechMetadata, fallback: stt.SpeechToTextEntity
    ) -> bool:
        candidate: object = fallback
        seen: set[int] = set()
        while candidate is not None:
            if isinstance(candidate, AssistLiveSTT) or id(candidate) in seen:
                return False
            seen.add(id(candidate))
            candidate = getattr(candidate, "fallback", None)
        return fallback.check_metadata(metadata)

    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        started = asyncio.get_running_loop().time()
        await self.ledger.prepare_speaker_read(self.satellite)
        turn = self.ledger.open_voice(self.satellite)
        failed = stt.SpeechResult("", stt.SpeechResultState.ERROR)
        fallback = self.resolve_fallback()
        if (
            metadata.format not in self.supported_formats
            or metadata.codec not in self.supported_codecs
            or metadata.bit_rate not in self.supported_bit_rates
            or metadata.sample_rate not in self.supported_sample_rates
            or metadata.channel not in self.supported_channels
            or (
                self.supported_languages
                and metadata.language not in self.supported_languages
            )
            or (
                fallback is not None
                and not self._fallback_compatible(metadata, fallback)
            )
        ):
            self.ledger.mark(turn.turn_id, "stt_failed")
            self.ledger.schedule_events()
            return failed
        audio = FallbackAudio(stream, started=started, cancelled=asyncio.Event())
        reason: FallbackReason = "server_unhealthy"
        error_class = "None"
        try:
            async with asyncio.timeout_at(started + 20):
                if self.ledger.save is not None:
                    await self.ledger.save()
                try:
                    async with asyncio.timeout(0.2):
                        health = await self.client.health_capabilities(timeout=0.2)
                        healthy = self.ledger.server_selected(
                            health, "stt", turn.turn_id
                        )
                        if not healthy:
                            reason = self.ledger.records[turn.turn_id].fallback_reasons[
                                "stt"
                            ]
                except (ProtocolError, TimeoutError, OSError) as error:
                    reason = (
                        "health_timeout"
                        if isinstance(error, TimeoutError)
                        or isinstance(error, ProtocolError)
                        and error.kind == "timeout"
                        else "health_error"
                    )
                    error_class = type(error).__name__
                    healthy = False
                if healthy:
                    connected = await self.ledger.connect(turn)
                    if self.ledger.current(self.satellite) != turn:
                        _LOGGER.debug("turn superseded during open")
                        return failed
                    if not connected:
                        record = self.ledger.records[turn.turn_id]
                        reason = record.open_failure
                        error_class = record.open_error_class
                        healthy = False
                if healthy:
                    self.ledger.used(turn.turn_id, "stt", "server", "unknown")
                    background = False
                    try:
                        messages = self.client.stt(turn.turn_id, audio.server_audio())
                        try:
                            while True:
                                async with asyncio.timeout(MESSAGE_DEADLINE):
                                    message = await anext(messages)
                                if message["type"] == "final":
                                    self.ledger.mark(turn.turn_id, "stt_final_received")
                                    speaker_deadline = (
                                        asyncio.get_running_loop().time()
                                        + SPEAKER_RESULT_DEADLINE
                                    )
                                    await self.ledger.prepare_speaker_read(
                                        self.satellite
                                    )
                                    self._start_speaker_read(
                                        messages, turn.turn_id, speaker_deadline
                                    )
                                    background = True
                                    return stt.SpeechResult(
                                        message["text"], stt.SpeechResultState.SUCCESS
                                    )
                        finally:
                            if not background and isinstance(messages, AsyncGenerator):
                                # A close error cannot invalidate a complete transcript.
                                with suppress(ProtocolError, TimeoutError, OSError):
                                    await messages.aclose()
                    except (
                        ProtocolError,
                        TimeoutError,
                        StopAsyncIteration,
                        OSError,
                    ) as error:
                        reason = stream_failure_reason(error)
                        error_class = type(error).__name__
                fallback = self.resolve_fallback()
                if fallback is None:
                    fallback_issue(
                        self.hass, self.entry_id, "stt", self.fallback_entity_id, True
                    )
                    await self.ledger.use_fallback(
                        turn.turn_id,
                        "stt_fallback",
                        self.fallback_entity_id,
                        reason="fallback_unavailable",
                        server_audio_samples=audio.server_audio_samples,
                        server_audio_last_chunk_samples=audio.server_audio_last_chunk_samples,
                    )
                    self.ledger.mark(turn.turn_id, "stt_failed")
                    return failed
                if not self._fallback_compatible(metadata, fallback):
                    self.ledger.mark(turn.turn_id, "stt_failed")
                    return failed
                await self.ledger.use_fallback(
                    turn.turn_id,
                    "stt_fallback",
                    self.fallback_entity_id,
                    reason=reason,
                    error_class=error_class,
                    server_audio_samples=audio.server_audio_samples,
                    server_audio_last_chunk_samples=audio.server_audio_last_chunk_samples,
                )

                async def local_audio() -> AsyncGenerator[bytes]:
                    async for chunk in audio.local_audio(MicroVad()):
                        yield chunk
                    self.ledger.mark(
                        turn.turn_id, "end_decision", decision_source="ha_audio_end"
                    )

                result = await fallback.async_process_audio_stream(
                    metadata, local_audio()
                )
                self.ledger.mark(
                    turn.turn_id,
                    "stt_final"
                    if result.result == stt.SpeechResultState.SUCCESS
                    else "stt_failed",
                )
                return (
                    result if result.result == stt.SpeechResultState.SUCCESS else failed
                )
        except Exception as error:
            _LOGGER.warning("STT turn failed: %s", type(error).__name__)
            self.ledger.mark(turn.turn_id, "stt_failed")
            return failed
        finally:
            await audio.aclose()
            self.ledger.schedule_events()
