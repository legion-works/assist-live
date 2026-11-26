import asyncio
import logging
import struct
from collections.abc import AsyncGenerator
from contextlib import suppress

from assist_live_client import AssistLiveClient
from assist_live_client.models import (
    HealthCapabilities,
    TTSAudio,
    TTSFormat,
    TTSSentenceEnd,
    TTSVoice,
)
from assist_live_client.protocol import ProtocolError
from homeassistant.components import tts
from homeassistant.components.tts.entity import TTSAudioRequest
from homeassistant.components.tts.helper import get_engine_instance
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AssistLiveConfigEntry
from .const import (
    CONF_FALLBACK_TTS_VOICE,
    CONF_NOTIFY_TTS_FAILURE,
    DOMAIN,
    HEALTH_TIMEOUT,
)
from .entity import AssistLiveEntity
from .ledger import FallbackReason, TurnLedger, stream_failure_reason
from .repairs import fallback_issue
from .settings import FallbackMetadata, effective_settings
from .tts_fallback import compatible_pcm

_LOGGER = logging.getLogger(__name__)

# No limit. Each reply uses its own server stream, not a shared device connection.
PARALLEL_UPDATES = 0
EARLY_START_DELAY = 1.0
EARLY_START_RATE = 22050
EARLY_START_PCM = bytes(5513 * 2)


def fallback_engine(hass: HomeAssistant, entity_id: str) -> tts.TextToSpeechEntity:
    try:
        engine = get_engine_instance(hass, entity_id)
    except KeyError:
        engine = None
    if not isinstance(engine, tts.TextToSpeechEntity):
        raise HomeAssistantError("fallback TTS unavailable")
    return engine


def language_matches(language: str, supported: str) -> bool:
    language = language.replace("_", "-").lower()
    supported = supported.replace("_", "-").lower()
    return language == supported or (
        language.split("-")[0] == supported.split("-")[0]
        and ("-" not in language or "-" not in supported)
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AssistLiveConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    settings = effective_settings(entry)
    cached = runtime.fallback_metadata.setdefault(
        "tts", {"entity_id": settings["fallback_tts"], "languages": []}
    )
    entities = []
    for device, satellite in entry.data["satellites"].items():
        entity = AssistLiveTTS(
            satellite,
            device,
            runtime.client,
            runtime.ledger,
            settings["fallback_tts"],
            entry.entry_id,
            cached,
        )
        entity.notify_tts_failure = entry.data.get(CONF_NOTIFY_TTS_FAILURE, False)
        entity.fallback_voice = settings.get(CONF_FALLBACK_TTS_VOICE) or None
        entity._attach_device(hass)
        runtime.tts[device] = entity
        entities.append(entity)
    async_add_entities(entities)


class AssistLiveTTS(AssistLiveEntity, tts.TextToSpeechEntity):
    _attr_supported_options = ["voice"]
    _attr_translation_key = "text_to_speech"

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
        self.fallback_voice: str | None = None
        self._refused_voices: dict[str, HealthCapabilities | None] = {}
        self._swept_voices: (
            tuple[tuple[tuple[str, str, str], ...], str | None] | None
        ) = None
        self.notify_tts_failure = False
        self._attr_unique_id = f"{satellite}_tts"
        self._set_device(device, satellite)
        self._fallback_metadata: FallbackMetadata = (
            fallback_metadata
            if fallback_metadata is not None
            else {"entity_id": fallback_entity_id, "languages": []}
        )

    def resolve_fallback(self) -> tts.TextToSpeechEntity | None:
        try:
            engine = fallback_engine(self.hass, self.fallback_entity_id)
        except HomeAssistantError:
            engine = None
        if engine is not None and not engine.available:
            engine = None
        if engine is not None and not isinstance(engine, AssistLiveTTS):
            self._fallback_metadata["languages"] = list(engine.supported_languages)
            self._fallback_metadata["default_language"] = engine.default_language
            self._fallback_metadata["default_options"] = {
                key: value
                for key, value in (engine.default_options or {}).items()
                if isinstance(value, str)
            }
        if engine is not None:
            fallback_issue(
                self.hass, self.entry_id, "tts", self.fallback_entity_id, False
            )
        return engine

    @property
    def server_voices(self) -> tuple[TTSVoice, ...]:
        health = self.ledger.last_health
        return health.tts_voices if isinstance(health, HealthCapabilities) else ()

    @property
    def supported_languages(self) -> list[str]:
        self.resolve_fallback()
        if not (voices := self.server_voices):
            return self._fallback_metadata["languages"]
        return sorted(
            {voice.language for voice in voices}
            | {voice.language.replace("_", "-").split("-")[0] for voice in voices}
        )

    @property
    def default_language(self) -> str:
        default = (self.default_options or {}).get("voice")
        for voice in self.server_voices:
            if voice.id == default:
                return voice.language
        if self.server_voices:
            return self.server_voices[0].language
        self.resolve_fallback()
        return self._fallback_metadata.get("default_language", "")

    @property
    def default_options(self) -> dict[str, str] | None:
        health = self.ledger.last_health
        if self.server_voices and isinstance(health, HealthCapabilities):
            if health.tts_default_voice is not None:
                return {"voice": health.tts_default_voice}
        return None

    @callback
    def _voice_issue(self, voice: str, active: bool) -> None:
        issue_id = f"{self.unique_id}_unknown_tts_voice_{voice}"
        if active:
            self._refused_voices[voice] = self.ledger.last_health
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="unknown_tts_voice",
                translation_placeholders={"voice": voice, "entity": self.entity_id},
            )
        else:
            self._refused_voices.pop(voice, None)
            if self.hass is not None:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @callback
    def async_update_voices(self) -> None:
        health = self.ledger.last_health
        voices = self.server_voices
        key = (
            tuple((voice.id, voice.name, voice.language) for voice in voices),
            health.tts_default_voice
            if isinstance(health, HealthCapabilities)
            else None,
        )
        if key == self._swept_voices:
            return
        self._swept_voices = key
        ids = {voice.id for voice in voices}
        for voice in ids:
            # Only a subsequent health response can clear a refused voice.
            if (
                voice not in self._refused_voices
                or self.ledger.last_health is not self._refused_voices[voice]
            ):
                self._voice_issue(voice, False)

    @callback
    def async_get_supported_voices(self, language: str) -> list[tts.Voice] | None:
        self.resolve_fallback()
        self.async_update_voices()
        return [
            tts.Voice(voice.id, voice.name)
            for voice in self.server_voices
            if language_matches(language, voice.language)
        ]

    async def async_stream_tts_audio(
        self, request: TTSAudioRequest
    ) -> tts.TTSAudioResponse:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + EARLY_START_DELAY
        engine = self.resolve_fallback()
        if (
            engine is not None
            and not self.server_voices
            and not any(
                language_matches(request.language, lang)
                for lang in engine.supported_languages
            )
        ):
            raise HomeAssistantError("fallback TTS language unavailable")
        if self.supported_languages and not any(
            language_matches(request.language, lang)
            for lang in self.supported_languages
        ):
            raise HomeAssistantError("TTS language unavailable")
        turn = self.ledger.open_tts(self.satellite)
        voice = request.options.get("voice")
        turn_record = self.ledger.records[turn.turn_id]
        turn_record.voice = voice
        if self.ledger.save is not None:
            await self.ledger.save()
        reason: FallbackReason = "server_unhealthy"
        error_class = "None"
        try:
            async with asyncio.timeout(HEALTH_TIMEOUT):
                health = await self.client.health_capabilities(timeout=HEALTH_TIMEOUT)
                healthy = self.ledger.server_selected(health, "tts", turn.turn_id)
                if not healthy:
                    reason = self.ledger.records[turn.turn_id].fallback_reasons["tts"]
                self.async_update_voices()
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
                self.ledger.schedule_events()
                raise HomeAssistantError("turn superseded during open")
            if not connected:
                record = self.ledger.records[turn.turn_id]
                reason = record.open_failure
                error_class = record.open_error_class
                healthy = False
        buffer: list[str] = []
        changed = asyncio.Condition()
        done = False
        failure: HomeAssistantError | None = None

        async def collect() -> None:
            nonlocal done, failure
            size = 0
            try:
                async for part in request.message_gen:
                    if not part:
                        continue
                    if len(part) > 16384 or size + len(part) > 16384:
                        failure = HomeAssistantError(
                            "TTS text limit is 16384 characters"
                        )
                        self.ledger.mark(turn.turn_id, "tts_failed")
                        return
                    size += len(part)
                    if not buffer:
                        self.ledger.mark(turn.turn_id, "tts_first_text")
                    async with changed:
                        buffer.append(part)
                        changed.notify_all()
            finally:
                async with changed:
                    done = True
                    changed.notify_all()

        producer = asyncio.create_task(collect())

        async def cleanup() -> None:
            producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer
            self.ledger.schedule_events()

        async def capture() -> AsyncGenerator[str]:
            offset = 0

            def available() -> bool:
                return len(buffer) > offset or done

            while True:
                async with changed:
                    await changed.wait_for(available)
                    if failure is not None:
                        raise failure
                    if len(buffer) == offset:
                        return
                    part = buffer[offset]
                    offset += 1
                yield part

        async def fresh() -> AsyncGenerator[str]:
            async for part in capture():
                yield part

        async def fallback(
            parts: AsyncGenerator[str] | None = None,
        ) -> tts.TTSAudioResponse:
            engine = self.resolve_fallback()
            if engine is None:
                fallback_issue(
                    self.hass, self.entry_id, "tts", self.fallback_entity_id, True
                )
                await self.ledger.use_fallback(
                    turn.turn_id,
                    "tts_fallback",
                    self.fallback_entity_id,
                    reason="fallback_unavailable",
                )
                raise HomeAssistantError("fallback TTS unavailable")
            fallback_language = next(
                (
                    lang
                    for lang in engine.supported_languages
                    if language_matches(request.language, lang)
                ),
                None,
            )
            if fallback_language is None:
                raise HomeAssistantError("fallback TTS language unavailable")
            options = {**(engine.default_options or {}), **request.options}
            options.pop("voice", None)
            fallback_voice = self.fallback_voice or (engine.default_options or {}).get(
                "voice"
            )
            if fallback_voice is not None:
                options["voice"] = fallback_voice
            self.ledger.records[turn.turn_id].voice = fallback_voice
            await self.ledger.use_fallback(
                turn.turn_id,
                "tts_fallback",
                self.fallback_entity_id,
                reason=reason,
                error_class=error_class,
            )
            return await engine.async_stream_tts_audio(
                TTSAudioRequest(
                    fallback_language,
                    options,
                    fresh() if parts is None else parts,
                )
            )

        if healthy:
            self.ledger.used(turn.turn_id, "tts", "server", "unknown")
        server = (
            self.client.tts_stream(
                turn.turn_id,
                capture(),
                voice=voice,
                text_positions=health.text_positions,
            )
            if healthy
            else None
        )
        audio_format: TTSFormat | None = None
        first_audio: TTSAudio | None = None
        pending: asyncio.Future[TTSAudio | TTSFormat | TTSSentenceEnd] | None = None
        early_start = False
        wav_header: bytes | None = None
        completed = 0
        text_end: int | None = None

        async def close_server() -> None:
            nonlocal pending
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                pending = None
            if isinstance(server, AsyncGenerator):
                await server.aclose()

        if server is not None:
            reason = "stream_closed"
            error_class = "None"
            try:
                while True:
                    if (
                        audio_format is not None
                        and audio_format.rate == EARLY_START_RATE
                    ):
                        # Cancellation of anext also closes the server stream.
                        pending = asyncio.ensure_future(anext(server))
                        ready, _ = await asyncio.wait(
                            (pending,), timeout=max(0, deadline - loop.time())
                        )
                        if not ready:
                            early_start = True
                            break
                        step, pending = pending, None
                        item = step.result()
                    else:
                        item = await anext(server)
                    if isinstance(item, TTSFormat):
                        if audio_format is not None:
                            raise ProtocolError("duplicate TTS format")
                        if (
                            item.codec != "pcm_s16le"
                            or item.width != 2
                            or item.channels != 1
                            or item.rate not in {16000, EARLY_START_RATE}
                        ):
                            raise ProtocolError("unsupported TTS audio format")
                        byte_rate = item.rate * item.channels * item.width
                        block_align = item.channels * item.width
                        audio_format = item
                        turn_record.audio_format = {
                            "codec": item.codec,
                            "rate": item.rate,
                            "channels": item.channels,
                            "width": item.width,
                        }
                    elif isinstance(item, TTSAudio):
                        if audio_format is None:
                            raise ProtocolError("TTS format required before audio")
                        if not item.data:
                            continue
                        first_audio = item
                        self.ledger.mark(turn.turn_id, "tts_audio_received")
                        break
                    elif isinstance(item, TTSSentenceEnd):
                        if audio_format is None:
                            raise ProtocolError(
                                "TTS format required before sentence end"
                            )
                        if item.sentence != completed:
                            raise ProtocolError("TTS sentence sequence mismatch")
                        completed += 1
                        text_end = item.text_end
                    else:
                        raise ProtocolError("invalid TTS stream item")
            except StopAsyncIteration:
                pass
            except (ProtocolError, TimeoutError) as error:
                reason = stream_failure_reason(error)
                error_class = type(error).__name__
                if (
                    isinstance(error, ProtocolError)
                    and str(error) == "unknown_tts_voice"
                    and voice is not None
                ):
                    self._voice_issue(voice, True)
                _LOGGER.warning("TTS stream failed: %s", type(error).__name__)
                healthy = False
            except Exception as error:
                reason = stream_failure_reason(error)
                error_class = type(error).__name__
                _LOGGER.warning("TTS stream failed: %s", type(error).__name__)
                healthy = False
            except BaseException:
                try:
                    await close_server()
                finally:
                    await cleanup()
                raise
            if first_audio is None and not early_start:
                healthy = False
            if healthy and audio_format is not None:
                try:
                    wav_header = struct.pack(
                        "<4sI4s4sIHHIIHH4sI",
                        b"RIFF",
                        0xFFFFFFFF,
                        b"WAVE",
                        b"fmt ",
                        16,
                        1,
                        audio_format.channels,
                        audio_format.rate,
                        byte_rate,
                        block_align,
                        16,
                        b"data",
                        0xFFFFFFFF,
                    )
                except Exception as error:
                    reason = "stream_error"
                    error_class = type(error).__name__
                    _LOGGER.warning("TTS header failed: %s", type(error).__name__)
                    healthy = False
            if not healthy:
                try:
                    await close_server()
                except BaseException:
                    await cleanup()
                    raise

        async def output() -> AsyncGenerator[bytes]:
            nonlocal completed, text_end, reason, error_class, pending
            assert server is not None and wav_header is not None
            invalid = False
            received_audio = first_audio is not None
            full: str | None = None
            try:
                if early_start:
                    # The first read is internal.
                    # Thus `aclose` can close an unread stream.
                    yield b""
                yield wav_header
                if early_start:
                    yield EARLY_START_PCM
                elif first_audio is not None:
                    yield first_audio.data
                while True:
                    try:
                        if pending is None:
                            item = await anext(server)
                        else:
                            step, pending = pending, None
                            item = await step
                    except StopAsyncIteration:
                        if early_start and not received_audio:
                            full = "".join([part async for part in fresh()])
                            if full.strip():
                                raise ProtocolError(
                                    "TTS stream ended before audio", kind="closed"
                                ) from None
                        break
                    if isinstance(item, TTSAudio):
                        if early_start and not received_audio:
                            if not item.data:
                                continue
                            self.ledger.mark(turn.turn_id, "tts_audio_received")
                            received_audio = True
                        yield item.data
                    elif isinstance(item, TTSSentenceEnd):
                        if item.sentence != completed:
                            invalid = True
                            raise ProtocolError("TTS sentence sequence mismatch")
                        completed += 1
                        text_end = item.text_end
                    elif isinstance(item, TTSFormat):
                        invalid = True
                        raise ProtocolError("duplicate TTS format")
                    else:
                        invalid = True
                        raise ProtocolError("invalid TTS stream item")
                if voice is not None:
                    self._voice_issue(voice, False)
            except Exception as error:
                reason = stream_failure_reason(error)
                error_class = type(error).__name__
                if (
                    isinstance(error, ProtocolError)
                    and str(error) == "unknown_tts_voice"
                    and voice is not None
                ):
                    self._voice_issue(voice, True)
                _LOGGER.warning("TTS stream failed: %s", type(error).__name__)
                if full is None:
                    full = "".join([part async for part in fresh()])
                before_audio = early_start and not received_audio
                if before_audio and not full.strip():
                    return
                suffix = (
                    full
                    if before_audio
                    else (full[text_end:].lstrip() if text_end is not None else None)
                )
                if (
                    (before_audio or not invalid)
                    and suffix is not None
                    and audio_format is not None
                ):
                    if not suffix and not before_audio:
                        self.ledger.late_server_failures += 1
                        return

                    async def rest() -> AsyncGenerator[str]:
                        yield suffix

                    try:
                        response = await fallback(rest())
                        first = True
                        async for chunk in compatible_pcm(response, audio_format):
                            if first and chunk:
                                turn_record.tts_audio_output = "in_stream_fallback"
                                self.ledger.mark(turn.turn_id, "tts_first_byte")
                                first = False
                            yield chunk
                        return
                    except Exception as fallback_error:
                        _LOGGER.warning(
                            "TTS fallback failed: %s", type(fallback_error).__name__
                        )
                self.ledger.mark(turn.turn_id, "tts_failed")
                self.hass.bus.async_fire(
                    "assist_live_tts_failed",
                    {"satellite": self.satellite, "text": full},
                )
                if self.notify_tts_failure:
                    try:
                        await self.hass.services.async_call(
                            "persistent_notification",
                            "create",
                            {"title": "assist-live TTS failed", "message": full},
                            blocking=True,
                        )
                    except Exception as notify_error:
                        _LOGGER.warning(
                            "TTS notification failed: %s", type(notify_error).__name__
                        )
                return
            finally:
                try:
                    await close_server()
                finally:
                    await cleanup()

        if not healthy:
            try:
                response = await fallback()
            except HomeAssistantError:
                full = "".join([part async for part in fresh()])
                self.ledger.mark(turn.turn_id, "tts_failed")
                self.hass.bus.async_fire(
                    "assist_live_tts_failed",
                    {"satellite": self.satellite, "text": full},
                )
                if self.notify_tts_failure:
                    try:
                        await self.hass.services.async_call(
                            "persistent_notification",
                            "create",
                            {"title": "assist-live TTS failed", "message": full},
                            blocking=True,
                        )
                    except Exception as notify_error:
                        _LOGGER.warning(
                            "TTS notification failed: %s", type(notify_error).__name__
                        )
                await cleanup()
                raise
            except BaseException:
                await cleanup()
                raise

            async def fallback_output() -> AsyncGenerator[bytes]:
                first = True
                try:
                    async for chunk in response.data_gen:
                        if first:
                            self.ledger.mark(turn.turn_id, "tts_first_byte")
                            first = False
                        yield chunk
                finally:
                    await cleanup()

            turn_record.tts_audio_output = "fallback"
            return tts.TTSAudioResponse(response.extension, fallback_output())
        stream = output()
        if early_start:
            await anext(stream)
            turn_record.reply_start = "early"
            turn_record.tts_audio_output = "early_start"
            self.ledger.early_reply_starts += 1
        else:
            turn_record.tts_audio_output = "streamed"
        return tts.TTSAudioResponse("wav", stream)
