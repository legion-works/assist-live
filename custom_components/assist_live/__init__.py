import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from datetime import timedelta
from typing import TypedDict

from assist_live_client import AssistLiveClient
from assist_live_client.protocol import ProtocolError
from homeassistant.components import assist_pipeline
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .ledger import TurnLedger
from .repairs import event_stream_issue, update_issues

PLATFORMS = [Platform.STT, Platform.TTS]
_LOGGER = logging.getLogger(__name__)


class SavedState(TypedDict):
    turns: list[dict[str, str]]
    cursors: dict[str, int]


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
        self.store = Store[SavedState | list[dict[str, str]]](
            hass, 1, f"{DOMAIN}.{entry.entry_id}.turns"
        )
        self.ledger.save = self._save
        self.cursors: dict[str, int] = {}
        self.failures: dict[str, int] = {}
        self.clock = hass.loop.time
        self._sleep = asyncio.sleep
        self._last_saved: SavedState | None = {"turns": [], "cursors": {}}
        self._last_write = 0.0
        self._save_lock = asyncio.Lock()
        self._health_due = 0.0
        self._healthy = False
        self._store_retry_due = 0.0

    async def _save(
        self, *, cursor: tuple[str, int] | None = None, debounce: bool = False
    ) -> None:
        async with self._save_lock:
            cursors = dict(self.cursors)
            if cursor is not None:
                cursors[cursor[0]] = cursor[1]
            data: SavedState = {"turns": self.ledger.snapshot(), "cursors": cursors}
            if data == self._last_saved:
                return
            if (
                debounce
                and self._last_saved is not None
                and data["turns"] == self._last_saved["turns"]
                and self.clock() - self._last_write < 5
            ):
                return
            await self.store.async_save(data)
            # HA Store logs some write failures without raising them.
            if await self.store.async_load() != data:
                raise HomeAssistantError("turn store write not confirmed")
            self._last_saved = data
            self._last_write = self.clock()
            self._store_retry_due = 0.0

    async def start(self) -> None:
        saved = await self.store.async_load()
        if isinstance(saved, list):
            await self.ledger.restore(saved)
            self._last_saved = None
        elif saved is not None:
            await self.ledger.restore(saved["turns"])
            self.cursors = {
                key: cursor
                for key, cursor in saved["cursors"].items()
                if key in self.entry.data["satellites"].values()
            }
            self._last_saved = saved
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
        while True:
            try:
                stream = self.client.events(
                    satellite, after_event_id=self.cursors.get(satellite, 0)
                )
                try:
                    async for envelope in stream:
                        event = envelope["event"]
                        if event["satellite"] != satellite:
                            raise ProtocolError("satellite event mismatch")
                        self.ledger.device_event(
                            satellite, event["turn_id"], event["stage"]
                        )
                        await self._save(
                            cursor=(satellite, envelope["event_id"]), debounce=True
                        )
                        self.cursors[satellite] = envelope["event_id"]
                        self.failures[satellite] = 0
                        event_stream_issue(
                            self.hass, self.entry.entry_id, satellite, False
                        )
                    raise ProtocolError("event stream ended")
                finally:
                    if isinstance(stream, AsyncGenerator):
                        await stream.aclose()
            except Exception:
                await self._sleep(self._note_failure(satellite))

    def _note_failure(self, satellite: str) -> float:
        failures = self.failures.get(satellite, 0) + 1
        self.failures[satellite] = failures
        delay = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)[min(failures - 1, 5)]
        _LOGGER.debug(
            "Event retry for %s: consecutive failures=%s; delay=%s s",
            satellite,
            failures,
            delay,
        )
        if failures >= 3:
            event_stream_issue(self.hass, self.entry.entry_id, satellite, True)
        return delay

    async def _maintenance(self, _: object) -> None:
        for satellite in self.entry.data["satellites"].values():
            self.ledger.current(satellite)
        self.ledger.prune()
        if self.clock() >= self._store_retry_due:
            try:
                await self._save(debounce=True)
            except Exception:
                delays = [
                    self._note_failure(satellite)
                    for satellite in self.entry.data["satellites"].values()
                ]
                self._store_retry_due = self.clock() + max(delays, default=30)
        await self.ledger.flush()
        if self.clock() >= self._health_due:
            self._healthy = await self.client.health(timeout=0.2)
            self._health_due = self.clock() + (30 if self._healthy else 5)
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
            self.hass, self.entry.entry_id, self._healthy, selected, forwarding_names
        )

    async def stop(self) -> None:
        for unsub in self.unsub:
            unsub()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        try:
            await self._save()
        finally:
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
    try:
        await runtime.start()
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
    except BaseException:
        try:
            await runtime.stop()
        except Exception:
            _LOGGER.debug("Runtime setup cleanup failed")
        hass.data[DOMAIN].pop(entry.entry_id)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = hass.data[DOMAIN].pop(entry.entry_id)
    await runtime.stop()
    return True
