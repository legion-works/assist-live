from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AssistLiveConfigEntry, Runtime
from .entity import AssistLiveEntity
from .ledger import TurnLedger

# No limit. The ledger pushes the state, and the sensor has no update call.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AssistLiveConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    entities = []
    for device, satellite in entry.data["satellites"].items():
        entity = TurnLatencySensor(satellite, device, runtime.ledger, runtime=runtime)
        entity._attach_device(hass)
        entities.append(entity)
    async_add_entities(entities)


class TurnLatencySensor(AssistLiveEntity, SensorEntity):
    _attr_should_poll = False
    _attr_translation_key = "last_turn_latency"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _unrecorded_attributes = frozenset({"turn_id", "turn_type"})

    def __init__(
        self,
        satellite: str,
        device: str,
        ledger: TurnLedger,
        *,
        runtime: Runtime | None = None,
    ) -> None:
        self.satellite = satellite
        self.ledger = ledger
        self._runtime = runtime
        self._attr_unique_id = f"{satellite}_last_turn_latency"
        self._set_device(device, satellite)

    @property
    def available(self) -> bool:
        return self._runtime is None or self._runtime.server_healthy

    def measurement(self) -> tuple[float | None, str, float | None]:
        record = self.ledger.latest.get(self.satellite)
        if record is None:
            return None, "missing", None
        start = record.timings.get("end_decision", {})
        end = record.timings.get("playback_start", {})
        if not start or not end or end["t_utc_ns"] < start["t_utc_ns"]:
            return None, "missing", None
        quality = (
            "measured" if start["quality"] == end["quality"] == "measured" else "proxy"
        )
        uncertainty = (
            None
            if start["uncertainty_ms"] is None or end["uncertainty_ms"] is None
            else start["uncertainty_ms"] + end["uncertainty_ms"]
        )
        return (end["t_utc_ns"] - start["t_utc_ns"]) / 1_000_000, quality, uncertainty

    @property
    def native_value(self) -> float | None:
        return self.measurement()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _, quality, uncertainty = self.measurement()
        record = self.ledger.latest.get(self.satellite)
        start = record.timings.get("end_decision", {}) if record else {}
        end = record.timings.get("playback_start", {}) if record else {}
        return {
            "turn_id": record.turn.turn_id if record else None,
            "turn_type": record.turn.turn_type if record else None,
            "quality": quality,
            "uncertainty_ms": uncertainty,
            "end_decision_source": start.get("decision_source")
            or start.get("source", "missing"),
            "end_decision_quality": start.get("quality", "missing"),
            "playback_quality": end.get("quality", "missing"),
        }

    async def async_added_to_hass(self) -> None:
        @callback
        def update() -> None:
            self.async_write_ha_state()

        self.ledger.listeners.add(update)
        self.async_on_remove(lambda: self.ledger.listeners.discard(update))
        if (runtime := self._runtime) is not None:
            runtime.health_listeners.add(update)
            self.async_on_remove(lambda: runtime.health_listeners.discard(update))
