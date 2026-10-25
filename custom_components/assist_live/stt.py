import asyncio
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from contextlib import suppress

from assist_live_client import AssistLiveClient
from assist_live_client.protocol import ProtocolError
from homeassistant.components import stt
from homeassistant.components.stt.models import SpeechAudioProcessing
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pymicro_vad import MicroVad

from .const import DOMAIN
from .ledger import TurnLedger


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

    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        turn = self.ledger.open_voice(self.satellite)
        failed = stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        if not self.check_metadata(metadata) or not self.fallback.check_metadata(
            metadata
        ):
            self.ledger.mark(turn.turn_id, "stt_failed")
            return failed
        source = aiter(stream)
        kept = bytearray()
        changed = asyncio.Condition()
        done = False

        async def collect() -> None:
            nonlocal done
            try:
                async with asyncio.timeout(15):
                    async for chunk in source:
                        async with changed:
                            kept.extend(chunk[: 480000 - len(kept)])
                            changed.notify_all()
                        if len(kept) >= 480000:
                            break
            except TimeoutError:
                pass
            finally:
                async with changed:
                    done = True
                    changed.notify_all()

        producer = asyncio.create_task(collect())

        async def server_audio() -> AsyncIterator[bytes]:
            offset = 0

            def available() -> bool:
                return len(kept) > offset or done

            while True:
                async with changed:
                    await changed.wait_for(available)
                    if len(kept) == offset:
                        return
                    chunk = bytes(kept[offset : offset + 32000])
                    offset += len(chunk)
                yield chunk

        async def local_audio() -> AsyncIterator[bytes]:
            vad = MicroVad()
            buffer = bytearray()
            spoken = False
            quiet = 0
            count = 0

            async for chunk in server_audio():
                buffer.extend(chunk)
                while len(buffer) >= 320:
                    frame = bytes(buffer[:320])
                    del buffer[:320]
                    probability = vad.Process10ms(frame)
                    if probability > 0.5:
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

        try:
            async with asyncio.timeout(20):
                if self.ledger.save is not None:
                    await self.ledger.save()
                if await self.client.health(timeout=0.2) and await self.ledger.connect(
                    turn
                ):
                    try:
                        messages = self.client.stt(turn.turn_id, server_audio())
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
                                await messages.aclose()
                    except ProtocolError, TimeoutError, StopAsyncIteration:
                        pass
                self.ledger.mark(turn.turn_id, "fallback_used")
                result = await self.fallback.async_process_audio_stream(
                    metadata, local_audio()
                )
                self.ledger.mark(
                    turn.turn_id,
                    "stt_final"
                    if result.result == stt.SpeechResultState.SUCCESS
                    else "stt_failed",
                )
                return result
        except ProtocolError, TimeoutError, HomeAssistantError:
            self.ledger.mark(turn.turn_id, "stt_failed")
            return failed
        finally:
            producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer
            await self.ledger.flush()
