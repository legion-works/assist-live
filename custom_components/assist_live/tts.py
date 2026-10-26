import asyncio
import logging
import struct
from collections.abc import AsyncGenerator
from contextlib import suppress

from assist_live_client import AssistLiveClient
from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd
from assist_live_client.protocol import ProtocolError
from homeassistant.components import tts
from homeassistant.components.tts.entity import TTSAudioRequest
from homeassistant.components.tts.helper import get_engine_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_NOTIFY_TTS_FAILURE, DOMAIN
from .ledger import TurnLedger
from .tts_fallback import compatible_pcm, remaining_text

_LOGGER = logging.getLogger(__name__)


def fallback_engine(hass: HomeAssistant, entity_id: str) -> tts.TextToSpeechEntity:
    engine = get_engine_instance(hass, entity_id)
    if not isinstance(engine, tts.TextToSpeechEntity):
        raise HomeAssistantError("fallback TTS unavailable")
    return engine


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    issue_id = f"{entry.entry_id}_fallback_tts_unavailable"
    try:
        fallback = fallback_engine(hass, entry.data["fallback_tts"])
    except HomeAssistantError:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="fallback_tts_unavailable",
        )
        raise
    ir.async_delete_issue(hass, DOMAIN, issue_id)
    entities = []
    for device, satellite in entry.data["satellites"].items():
        entity = AssistLiveTTS(
            satellite, device, runtime.client, runtime.ledger, fallback
        )
        entity.notify_tts_failure = entry.data.get(CONF_NOTIFY_TTS_FAILURE, False)
        if registered := dr.async_get(hass).async_get(device):
            entity._attr_device_info = dr.DeviceInfo(
                identifiers=registered.identifiers,
                connections=registered.connections
                if isinstance(registered, dr.DeviceEntry)
                else set(),
            )
        runtime.tts[device] = entity
        entities.append(entity)
    async_add_entities(entities)


class AssistLiveTTS(tts.TextToSpeechEntity):
    _attr_supported_options = ["voice"]

    def __init__(
        self,
        satellite: str,
        device: str,
        client: AssistLiveClient,
        ledger: TurnLedger,
        fallback: tts.TextToSpeechEntity,
    ) -> None:
        self.satellite = satellite
        self.client = client
        self.ledger = ledger
        self.fallback = fallback
        self.notify_tts_failure = False
        self._attr_unique_id = f"{satellite}_tts"
        self._attr_name = f"assist-live {satellite}"
        self._attr_device_info = {"identifiers": {(DOMAIN, device)}}
        self._attr_supported_languages = fallback.supported_languages
        self._attr_default_language = fallback.default_language

    @callback
    def async_get_supported_voices(self, language: str) -> list[tts.Voice] | None:
        return self.fallback.async_get_supported_voices(language)

    async def async_stream_tts_audio(
        self, request: TTSAudioRequest
    ) -> tts.TTSAudioResponse:
        if (
            request.language not in self.supported_languages
            or request.language not in self.fallback.supported_languages
        ):
            raise HomeAssistantError("fallback TTS language unavailable")
        turn = self.ledger.open_tts(self.satellite)
        if self.ledger.save is not None:
            await self.ledger.save()
        try:
            async with asyncio.timeout(0.2):
                health = await self.client.health_capabilities(timeout=0.2)
                healthy = health.healthy is True and health.tts is True
        except ProtocolError, TimeoutError, OSError:
            healthy = False
        healthy = healthy and await self.ledger.connect(turn)
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
            await self.ledger.flush()

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
            self.ledger.mark(turn.turn_id, "fallback_used")
            return await self.fallback.async_stream_tts_audio(
                TTSAudioRequest(
                    request.language,
                    request.options,
                    fresh() if parts is None else parts,
                )
            )

        server = (
            self.client.tts_stream(
                turn.turn_id,
                capture(),
                voice=request.options["voice"]
                if "voice" in request.options
                else (self.fallback.default_options or {}).get("voice", ""),
            )
            if healthy
            else None
        )
        audio_format: TTSFormat | None = None
        first_audio: TTSAudio | None = None
        completed = 0
        if server is not None:
            try:
                async for item in server:
                    if isinstance(item, TTSFormat):
                        if audio_format is not None:
                            raise ProtocolError("duplicate TTS format")
                        if (
                            item.codec != "pcm_s16le"
                            or item.width != 2
                            or item.channels != 1
                            or item.rate not in {16000, 22050}
                        ):
                            raise ProtocolError("unsupported TTS audio format")
                        byte_rate = item.rate * item.channels * item.width
                        block_align = item.channels * item.width
                        audio_format = item
                    elif isinstance(item, TTSAudio):
                        if audio_format is None:
                            raise ProtocolError("TTS format required before audio")
                        first_audio = item
                        break
                    elif isinstance(item, TTSSentenceEnd):
                        if audio_format is None:
                            raise ProtocolError(
                                "TTS format required before sentence end"
                            )
                        if item.sentence != completed:
                            raise ProtocolError("TTS sentence sequence mismatch")
                        completed += 1
                    else:
                        raise ProtocolError("invalid TTS stream item")
            except (ProtocolError, TimeoutError, StopAsyncIteration) as error:
                _LOGGER.warning("TTS stream failed: %s", type(error).__name__)
                healthy = False
            except Exception as error:
                _LOGGER.warning("TTS stream failed: %s", type(error).__name__)
                healthy = False
            except BaseException:
                await cleanup()
                raise
            if first_audio is None:
                healthy = False
            wav_header: bytes | None = None
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
                    _LOGGER.warning("TTS header failed: %s", type(error).__name__)
                    healthy = False
            if not healthy and isinstance(server, AsyncGenerator):
                await server.aclose()

        async def output() -> AsyncGenerator[bytes]:
            nonlocal completed
            assert server is not None and wav_header is not None
            invalid = False
            try:
                self.ledger.mark(turn.turn_id, "tts_first_byte")
                yield wav_header
                if first_audio is not None:
                    yield first_audio.data
                async for item in server:
                    if isinstance(item, TTSAudio):
                        yield item.data
                    elif isinstance(item, TTSSentenceEnd):
                        if item.sentence != completed:
                            invalid = True
                            raise ProtocolError("TTS sentence sequence mismatch")
                        completed += 1
                    elif isinstance(item, TTSFormat):
                        invalid = True
                        raise ProtocolError("duplicate TTS format")
                    else:
                        invalid = True
                        raise ProtocolError("invalid TTS stream item")
            except Exception as error:
                _LOGGER.warning("TTS stream failed: %s", type(error).__name__)
                full = "".join([part async for part in fresh()])
                suffix = remaining_text(full, completed)
                if not invalid and suffix is not None and audio_format is not None:
                    if not suffix:
                        return

                    async def rest() -> AsyncGenerator[str]:
                        yield suffix

                    try:
                        response = await fallback(rest())
                        async for chunk in compatible_pcm(response, audio_format):
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
                if isinstance(server, AsyncGenerator):
                    await server.aclose()
                await cleanup()

        if not healthy:
            try:
                response = await fallback()
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

            return tts.TTSAudioResponse(response.extension, fallback_output())
        return tts.TTSAudioResponse("wav", output())
