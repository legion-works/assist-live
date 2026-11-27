import asyncio
import copy
import logging
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import NotRequired, TypedDict

from assist_live_client import AssistLiveClient
from assist_live_client.models import HealthCapabilities, TimingEvent
from assist_live_client.protocol import ProtocolError
from homeassistant.components import assist_pipeline
from homeassistant.components.assist_satellite.entity import AssistSatelliteState
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassEntryKey

from .const import CONF_FALLBACK_TTS_VOICE, DOMAIN, HEALTH_TIMEOUT
from .ledger import TurnLedger
from .repairs import (
    event_stream_issue,
    fallback_issue,
    speaker_destination_issue,
    stored_state_issue,
    update_issues,
)
from .routing import selected_pipelines
from .settings import FallbackMetadata, effective_settings
from .stt_hints import STTHints

PLATFORMS = [Platform.STT, Platform.TTS, Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


class SavedState(TypedDict):
    version: int
    turns: list[dict[str, str]]
    cursors: dict[str, int]
    fallback_metadata: NotRequired[dict[str, FallbackMetadata]]


_UNSAVED_STATE = HassEntryKey[SavedState](f"{DOMAIN}.unsaved_state")


class Runtime:
    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: AssistLiveClient
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self._unsaved_state = hass.data.setdefault(_UNSAVED_STATE, {})
        self.hints = STTHints(hass, client, entry.data["satellites"].values())
        self.ledger = TurnLedger(client)
        self.ledger.create_event_task = lambda coro: hass.async_create_background_task(
            coro, "assist-live-timing-events"
        )
        self.ledger.fallback_policy = self.fallback_destination
        self.ledger.speaker_event = self.speaker_metadata
        self.ledger.speaker_destination = self.speaker_destination
        self.speaker_policy_violations = 0
        self.stt: dict[str, object] = {}
        self.tts: dict[str, object] = {}
        self.update_options(entry)
        self.tasks: list[asyncio.Task[None]] = []
        self.unsub: list[Callable[[], None]] = []
        self.store = Store[SavedState | list[dict[str, str]]](
            hass, 1, f"{DOMAIN}.{entry.entry_id}.turns"
        )
        self.ledger.save = self._save
        self.cursors: dict[str, int] = {}
        self.fallback_metadata: dict[str, FallbackMetadata] = {}
        self.failures: dict[str, int] = {}
        self.clock = hass.loop.time
        self._sleep = asyncio.sleep
        self._last_saved: SavedState | None = {"version": 1, "turns": [], "cursors": {}}
        self._last_write = 0.0
        self._save_lock = asyncio.Lock()
        self._health_due = 0.0
        self._healthy: bool | None = None
        self.health_listeners: set[Callable[[], None]] = set()
        self.last_healthy: datetime | None = None
        self.last_healthy_health: HealthCapabilities | None = None
        self._store_retry_due = 0.0

    @property
    def server_healthy(self) -> bool:
        return self._healthy is True

    @callback
    def update_options(self, entry: ConfigEntry) -> None:
        from .tts import AssistLiveTTS

        settings = effective_settings(entry)
        self.ledger.record_transcripts = settings.get("record_transcripts", False)
        self.ledger.local_only = settings.get("local_only", False)
        for entity in self.tts.values():
            if isinstance(entity, AssistLiveTTS):
                entity.fallback_voice = settings.get(CONF_FALLBACK_TTS_VOICE) or None

    async def fallback_destination(self, entity_id: str) -> str:
        from .config_flow import destination, entity_iot_class

        mode = destination(await entity_iot_class(self.hass, entity_id))
        settings = effective_settings(self.entry)
        if (
            mode == "unknown"
            and settings.get("local_decisions", {})
            .get(entity_id, {})
            .get("destination")
            == "local"
        ):
            mode = "local"
        if self.ledger.local_only and mode != "local":
            raise HomeAssistantError("fallback destination refused")
        return mode

    @callback
    def speaker_destination(self, data: dict[str, object]) -> None:
        if self.ledger.local_only and data["destination"] != "local":
            self.speaker_policy_violations += 1
            speaker_destination_issue(self.hass, self.entry.entry_id)
        elif data["destination"] == "local":
            speaker_destination_issue(self.hass, self.entry.entry_id, False)

    @callback
    def speaker_metadata(self, data: dict[str, object]) -> None:
        self.speaker_destination(data)
        self.hass.bus.async_fire("assist_live_speaker", data)

    def _saved_state(self) -> SavedState:
        data: SavedState = {
            "version": 1,
            "turns": self.ledger.snapshot(),
            "cursors": dict(self.cursors),
        }
        if self.fallback_metadata:
            data["fallback_metadata"] = copy.deepcopy(self.fallback_metadata)
        return data

    async def _save(
        self, *, cursor: tuple[str, int] | None = None, debounce: bool = False
    ) -> None:
        async with self._save_lock:
            data = self._saved_state()
            if cursor is not None:
                data["cursors"][cursor[0]] = cursor[1]
            if data == self._last_saved:
                return
            if (
                debounce
                and self._last_saved is not None
                and data["turns"] == self._last_saved["turns"]
                and data.get("fallback_metadata")
                == self._last_saved.get("fallback_metadata")
                and self.clock() - self._last_write < 5
            ):
                return
            await self.store.async_save(data)
            # HA Store logs some write failures without raising them.
            if await self.store.async_load() != data:
                raise HomeAssistantError("turn store write not confirmed")
            self._unsaved_state.pop(self.entry.entry_id, None)
            self._last_saved = data
            self._last_write = self.clock()
            self._store_retry_due = 0.0

    async def start(self) -> None:
        saved = self._unsaved_state.get(self.entry.entry_id)
        await self._restore_saved(
            saved if saved is not None else await self.store.async_load()
        )
        self.hints.start()
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
            if (
                entity
                and state
                and state.state in AssistSatelliteState
                and entity.device_id in self.entry.data["satellites"]
            ):
                satellite = self.entry.data["satellites"][entity.device_id]
                self.ledger.satellite_state(satellite, state.state)
                self.ledger.schedule_events()

        self.unsub.append(
            async_track_state_change_event(self.hass, entities, state_changed)
        )
        self.unsub.append(
            async_track_time_interval(
                self.hass, self._maintenance, timedelta(seconds=1)
            )
        )

    async def _restore_saved(self, saved: object) -> None:
        if saved is None:
            return
        invalid = False
        rows: object = []
        cursors: object = {}
        metadata: object = {}
        if isinstance(saved, list):
            rows = saved
        elif (
            isinstance(saved, dict)
            and type(saved.get("version", 0)) is int
            and saved.get("version", 0) in (0, 1)
        ):
            rows, cursors = saved.get("turns"), saved.get("cursors")
            metadata = saved.get("fallback_metadata", {})
        else:
            invalid = True
        if not isinstance(rows, list):
            rows = []
            invalid = True
        valid_rows: list[dict[str, str]] = []
        satellites = set(self.entry.data["satellites"].values())
        for row in rows:
            try:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("turn_id"), str)
                    or row.get("satellite") not in satellites
                ):
                    raise ValueError("invalid stored turn")
                TimingEvent(
                    row["turn_id"],
                    row["satellite"],
                    "integration",
                    "turn_superseded",
                    time.time_ns(),
                    "measured",
                    None,
                )
                valid_rows.append(
                    {"turn_id": row["turn_id"], "satellite": row["satellite"]}
                )
            except KeyError, TypeError, ValueError:
                invalid = True
        if not isinstance(cursors, dict):
            cursors = {}
            invalid = True
        for key, value in cursors.items():
            if key in satellites and type(value) is int and 0 <= value < 1 << 64:
                self.cursors[key] = value
            else:
                invalid = True
        settings = effective_settings(self.entry)
        if not isinstance(metadata, dict):
            metadata = {}
            invalid = True
        for part, values in metadata.items():
            if part not in ("stt", "tts"):
                continue
            if (
                not isinstance(values, dict)
                or not isinstance(values.get("entity_id"), str)
                or not isinstance(values.get("languages"), list)
                or not all(isinstance(lang, str) for lang in values["languages"])
                or (
                    "default_language" in values
                    and not isinstance(values["default_language"], str)
                )
                or (
                    "default_options" in values
                    and (
                        not isinstance(values["default_options"], dict)
                        or not all(
                            isinstance(key, str) and isinstance(value, str)
                            for key, value in values["default_options"].items()
                        )
                    )
                )
            ):
                invalid = True
                continue
            if values["entity_id"] != settings.get(f"fallback_{part}"):
                continue
            cached: FallbackMetadata = {
                "entity_id": values["entity_id"],
                "languages": list(values["languages"]),
            }
            if "default_language" in values:
                cached["default_language"] = values["default_language"]
            if "default_options" in values:
                cached["default_options"] = dict(values["default_options"])
            self.fallback_metadata[part] = cached
        await self.ledger.restore(valid_rows)
        self._last_saved = None
        if invalid:
            _LOGGER.warning("Dropped invalid stored turn data")
        stored_state_issue(self.hass, self.entry.entry_id, invalid)

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
                        self.ledger.observe(event)
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

    async def refresh_health(self) -> None:
        from .stt import AssistLiveSTT
        from .tts import AssistLiveTTS

        if self.clock() < self._health_due:
            return
        for part, entities in (("stt", self.stt), ("tts", self.tts)):
            for entity in entities.values():
                if isinstance(entity, (AssistLiveSTT, AssistLiveTTS)):
                    engine = entity.resolve_fallback()
                    if engine is None and self.hass.state is CoreState.running:
                        fallback_issue(
                            self.hass,
                            self.entry.entry_id,
                            part,
                            entity.fallback_entity_id,
                            True,
                        )
        try:
            health = await self.client.health_capabilities(timeout=HEALTH_TIMEOUT)
        except ProtocolError, TimeoutError, OSError:
            healthy = False
        else:
            healthy = False
            if isinstance(health, HealthCapabilities):
                healthy = health.healthy is True
                if healthy:
                    self.last_healthy = dt_util.utcnow()
                    self.last_healthy_health = health
                    self.ledger.schedule_events(retry=True)
                self.ledger.last_health = health
                self.hints.observe_health(health)
                for entity in self.tts.values():
                    if isinstance(entity, AssistLiveTTS):
                        entity.async_update_voices()
        self._health_due = self.clock() + (30 if healthy else 5)
        previous, self._healthy = self._healthy, healthy
        if healthy != previous:
            if not healthy:
                _LOGGER.info("Assist-live server is unavailable")
            elif previous is False:
                _LOGGER.info("Assist-live server is available again")
            for listener in tuple(self.health_listeners):
                listener()

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
        await self.refresh_health()
        entry_selectors = self.entry.data.get("pipeline_selects", {})
        if not entry_selectors:
            update_issues(
                self.hass, self.entry.entry_id, self.server_healthy, [], set()
            )
            return
        pipelines = assist_pipeline.async_get_pipelines(self.hass)
        entries = {
            entry.entry_id: entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        }
        entries[self.entry.entry_id] = self.entry
        selectors = {
            device: entity_id
            for entry in entries.values()
            for device, entity_id in entry.data.get("pipeline_selects", {}).items()
        }
        resolved = selected_pipelines(self.hass, selectors.values(), pipelines)
        owned = {
            pipeline_id
            for entry in entries.values()
            for pipeline_id in entry.data.get("pipelines", {}).values()
        }
        entry_selections = {
            pipeline.id
            for entity_id in entry_selectors.values()
            if (pipeline := resolved.get(entity_id)) is not None
        }
        registry = er.async_get(self.hass)
        forwarding_ids = {
            p.id
            for p in pipelines
            if p.id in entry_selections
            and (
                p.id in owned
                or any(
                    engine_id is not None
                    and (entity := registry.async_get(engine_id)) is not None
                    and entity.platform == DOMAIN
                    for engine_id in (p.stt_engine, p.tts_engine)
                )
            )
        }
        selected = [
            pipeline.id
            for entity_id in selectors.values()
            if (pipeline := resolved.get(entity_id)) is not None
        ]
        update_issues(
            self.hass,
            self.entry.entry_id,
            self.server_healthy,
            selected,
            forwarding_ids,
        )

    async def stop(self) -> None:
        await self.hints.stop()
        await self.ledger.stop_events()
        await self.ledger.stop_speaker_reads()
        for unsub in self.unsub:
            unsub()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        try:
            await self._save()
        except Exception as error:
            # HA skips setup after an unload error. An HA restart clears this state.
            self._unsaved_state[self.entry.entry_id] = self._saved_state()
            _LOGGER.warning("Final turn store save failed: %s", type(error).__name__)
        finally:
            await self.client.close()


type AssistLiveConfigEntry = ConfigEntry[Runtime]


def _can_remove_device(
    hass: HomeAssistant, entry: AssistLiveConfigEntry, device: dr.AnyDeviceEntry
) -> bool:
    return (
        device.config_entry_id == entry.entry_id
        and device.id not in entry.data["satellites"]
        and not dr.async_entries_for_parent_device(dr.async_get(hass), device.id)
        and not er.async_entries_for_device(
            er.async_get(hass), device.id, include_disabled_entities=True
        )
    )


async def async_setup_entry(hass: HomeAssistant, entry: AssistLiveConfigEntry) -> bool:
    from .config_flow import is_forwarder
    from .stt import AssistLiveSTT
    from .tts import AssistLiveTTS

    settings = effective_settings(entry)
    if any(
        is_forwarder(hass, settings[name]) for name in ("fallback_stt", "fallback_tts")
    ):
        return False
    client = AssistLiveClient(
        entry.data["url"], entry.data["token"], session=async_get_clientsession(hass)
    )
    runtime = Runtime(hass, entry, client)
    entry.runtime_data = runtime
    forwarding_started = False
    try:
        await runtime.start()
        await runtime.refresh_health()

        async def options_updated(hass: HomeAssistant, updated: ConfigEntry) -> None:
            current = effective_settings(updated)
            if any(
                current[name] != settings[name]
                for name in ("fallback_stt", "fallback_tts")
            ):
                await hass.config_entries.async_reload(updated.entry_id)
            else:
                runtime.update_options(updated)

        entry.async_on_unload(entry.add_update_listener(options_updated))
        forwarding_started = True
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
        devices = dr.async_get(hass)
        for device_entry in dr.async_entries_for_config_entry(devices, entry.entry_id):
            if _can_remove_device(hass, entry, device_entry):
                devices.async_remove_device(device_entry.id)
    except BaseException:
        # Cleanup must not replace the setup error or cancellation.
        if forwarding_started:
            try:
                await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            except BaseException:
                _LOGGER.debug("Platform setup cleanup failed")
        try:
            await runtime.stop()
        except BaseException:
            _LOGGER.debug("Runtime setup cleanup failed")
        # HA removes runtime_data after an unload, but not after a setup error.
        with suppress(AttributeError):
            del entry.runtime_data
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AssistLiveConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.stop()
    settings = effective_settings(entry)
    for part in ("stt", "tts"):
        fallback_issue(
            hass, entry.entry_id, part, settings.get(f"fallback_{part}", ""), False
        )
    return True


async def async_remove_entry(hass: HomeAssistant, entry: AssistLiveConfigEntry) -> None:
    if (unsaved := hass.data.get(_UNSAVED_STATE)) is not None:
        unsaved.pop(entry.entry_id, None)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: AssistLiveConfigEntry, device: dr.AnyDeviceEntry
) -> bool:
    return _can_remove_device(hass, entry, device)
