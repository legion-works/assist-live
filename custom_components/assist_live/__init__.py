import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta

from assist_live_client import AssistLiveClient
from assist_live_client.protocol import ProtocolError
from homeassistant.components import assist_pipeline
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .ledger import TurnLedger
from .repairs import update_issues

PLATFORMS = [Platform.STT, Platform.TTS]


class Runtime:
    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: AssistLiveClient
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.ledger = TurnLedger(client)
        self.stt: dict[str, object] = {}
        self.tts: dict[str, object] = {}
        self.tasks: list[asyncio.Task[None]] = []
        self.unsub: list[Callable[[], None]] = []
        self.store = Store[list[dict[str, str]]](
            hass, 1, f"{DOMAIN}.{entry.entry_id}.turns"
        )
        self.ledger.save = self._save

    async def _save(self) -> None:
        await self.store.async_save(self.ledger.snapshot())

    async def start(self) -> None:
        await self.ledger.restore(await self.store.async_load() or [])
        for satellite in self.entry.data["satellites"].values():
            self.tasks.append(
                self.hass.async_create_background_task(
                    self._events(satellite),
                    f"assist-live-events-{satellite}",
                )
            )
        registry = er.async_get(self.hass)
        entities = [
            entity.entity_id
            for entity in registry.entities.values()
            if entity.device_id in self.entry.data["satellites"]
            and entity.domain == "assist_satellite"
        ]

        @callback
        def state_changed(event: Event[EventStateChangedData]) -> None:
            entity = registry.async_get(event.data["entity_id"])
            state = event.data.get("new_state")
            if entity and state and entity.device_id in self.entry.data["satellites"]:
                satellite = self.entry.data["satellites"][entity.device_id]
                self.ledger.satellite_state(satellite, state.state)
                self.hass.async_create_task(self.ledger.flush())

        self.unsub.append(
            async_track_state_change_event(self.hass, entities, state_changed)
        )
        self.unsub.append(
            async_track_time_interval(
                self.hass, self._maintenance, timedelta(seconds=1)
            )
        )

    async def _events(self, satellite: str) -> None:
        cursor = 0
        while True:
            try:
                async for envelope in self.client.events(
                    satellite, after_event_id=cursor
                ):
                    event = envelope["event"]
                    if event["satellite"] != satellite:
                        raise ProtocolError("satellite event mismatch")
                    self.ledger.device_event(
                        satellite, event["turn_id"], event["stage"]
                    )
                    cursor = envelope["event_id"]
                    await self.store.async_save(self.ledger.snapshot())
            except ProtocolError:
                pass
            await asyncio.sleep(1)

    async def _maintenance(self, _: object) -> None:
        for satellite in self.entry.data["satellites"].values():
            self.ledger.current(satellite)
        await self.store.async_save(self.ledger.snapshot())
        await self.ledger.flush()
        healthy = await self.client.health(timeout=0.2)
        pipelines = assist_pipeline.async_get_pipelines(self.hass)
        registry = er.async_get(self.hass)
        forwarding_names = {
            p.name
            for p in pipelines
            if p.id in self.entry.data.get("pipelines", {}).values()
            or any(
                engine_id is not None
                and (entity := registry.async_get(engine_id)) is not None
                and entity.platform == DOMAIN
                for engine_id in (p.stt_engine, p.tts_engine)
            )
        }
        selected = [
            state.state
            for entity_id in self.entry.data.get("pipeline_selects", {}).values()
            if (state := self.hass.states.get(entity_id)) is not None
        ]
        update_issues(
            self.hass, self.entry.entry_id, healthy, selected, forwarding_names
        )

    async def stop(self) -> None:
        for unsub in self.unsub:
            unsub()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        await self.store.async_save(self.ledger.snapshot())
        await self.client.close()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .config_flow import is_forwarder
    from .stt import AssistLiveSTT
    from .tts import AssistLiveTTS

    if any(
        is_forwarder(hass, entry.data[name])
        for name in ("fallback_stt", "fallback_tts")
    ):
        return False
    client = AssistLiveClient(
        entry.data["url"], entry.data["token"], session=async_get_clientsession(hass)
    )
    runtime = Runtime(hass, entry, client)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    await runtime.start()
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        saved = dict(entry.data.get("pipelines", {}))
        pipelines = {p.id: p for p in assist_pipeline.async_get_pipelines(hass)}
        for device, satellite in entry.data["satellites"].items():
            stt_entity = runtime.stt[device]
            tts_entity = runtime.tts[device]
            if not isinstance(stt_entity, AssistLiveSTT) or not isinstance(
                tts_entity, AssistLiveTTS
            ):
                continue
            if device in saved:
                continue
            pipeline = await assist_pipeline.async_create_default_pipeline(
                hass,
                stt_entity.entity_id,
                tts_entity.entity_id,
                f"assist-live {satellite}",
            )
            if pipeline is None:
                continue
            saved[device] = pipeline.id
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "pipelines": saved}
            )
            pipelines[pipeline.id] = pipeline
            voice = entry.data.get("voices", {}).get(device)
            if voice is not None:
                await assist_pipeline.async_update_pipeline(
                    hass, pipeline, tts_voice=voice
                )
        await runtime._maintenance(None)
    except Exception:
        await runtime.stop()
        hass.data[DOMAIN].pop(entry.entry_id)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = hass.data[DOMAIN].pop(entry.entry_id)
    await runtime.stop()
    return True
