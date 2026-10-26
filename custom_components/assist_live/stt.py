import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import suppress

from assist_live_client import AssistLiveClient
from assist_live_client.protocol import ProtocolError
from homeassistant.components import stt
from homeassistant.components.stt.models import SpeechAudioProcessing
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pymicro_vad import MicroVad

from .const import DOMAIN
from .ledger import TurnLedger
from .stt_fallback import FallbackAudio

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    fallback = stt.async_get_speech_to_text_entity(hass, entry.data["fallback_stt"])
    if fallback is None:
        raise ValueError("fallback STT unavailable")
    entities = []
    for device, satellite in entry.data["satellites"].items():
        entity = AssistLiveSTT(
            satellite, device, runtime.client, runtime.ledger, fallback
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
        fallback: stt.SpeechToTextEntity,
    ) -> None:
        self.satellite = satellite
        self.client = client
        self.ledger = ledger
        self.fallback = fallback
        self._languages = list(fallback.supported_languages)
        self._attr_unique_id = f"{satellite}_stt"
        self._attr_name = f"assist-live {satellite}"
        self._attr_device_info = {"identifiers": {(DOMAIN, device)}}

    @property
    def supported_languages(self) -> list[str]:
        return self._languages

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

    def _fallback_compatible(self, metadata: stt.SpeechMetadata) -> bool:
        candidate: object = self.fallback
        seen: set[int] = set()
        while candidate is not None:
            if isinstance(candidate, AssistLiveSTT) or id(candidate) in seen:
                return False
            seen.add(id(candidate))
            candidate = getattr(candidate, "fallback", None)
        return self.fallback.check_metadata(metadata)

    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        started = asyncio.get_running_loop().time()
        turn = self.ledger.open_voice(self.satellite)
        failed = stt.SpeechResult("", stt.SpeechResultState.ERROR)
        if not self.check_metadata(metadata) or not self._fallback_compatible(metadata):
            self.ledger.mark(turn.turn_id, "stt_failed")
            await self.ledger.flush()
            return failed
        audio = FallbackAudio(stream, started=started, cancelled=asyncio.Event())
        try:
            async with asyncio.timeout_at(started + 20):
                if self.ledger.save is not None:
                    await self.ledger.save()
                try:
                    async with asyncio.timeout(0.2):
                        health = await self.client.health_capabilities(timeout=0.2)
                        healthy = health.healthy is True and health.stt is True
                except ProtocolError, TimeoutError, OSError:
                    healthy = False
                if healthy and await self.ledger.connect(turn):
                    try:
                        messages = self.client.stt(turn.turn_id, audio.server_audio())
                        try:
                            while True:
                                async with asyncio.timeout(0.6):
                                    message = await anext(messages)
                                if message["type"] == "final":
                                    self.ledger.mark(turn.turn_id, "stt_final")
                                    return stt.SpeechResult(
                                        message["text"], stt.SpeechResultState.SUCCESS
                                    )
                        finally:
                            if isinstance(messages, AsyncGenerator):
                                # A close error cannot invalidate a complete transcript.
                                with suppress(ProtocolError, TimeoutError, OSError):
                                    await messages.aclose()
                    except ProtocolError, TimeoutError, StopAsyncIteration, OSError:
                        pass
                if not self._fallback_compatible(metadata):
                    self.ledger.mark(turn.turn_id, "stt_failed")
                    return failed
                self.ledger.mark(turn.turn_id, "fallback_used")
                result = await self.fallback.async_process_audio_stream(
                    metadata, audio.local_audio(MicroVad())
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
            await self.ledger.flush()
