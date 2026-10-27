from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .ledger import TurnLedger


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    ledger = hass.data[DOMAIN][entry.entry_id].ledger
    entities = []
    for device, satellite in entry.data["satellites"].items():
        entity = TurnLatencySensor(satellite, device, ledger)
        if registered := dr.async_get(hass).async_get(device):
            entity._attr_device_info = dr.DeviceInfo(
                identifiers=registered.identifiers,
                connections=registered.connections
                if isinstance(registered, dr.DeviceEntry)
                else set(),
            )
        entities.append(entity)
    async_add_entities(entities)


class TurnLatencySensor(SensorEntity):
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS

    def __init__(self, satellite: str, device: str, ledger: TurnLedger) -> None:
        self.satellite = satellite
        self.ledger = ledger
        self._attr_unique_id = f"{satellite}_last_turn_latency"
        self._attr_name = f"assist-live {satellite} last turn latency"
        self._attr_device_info = {"identifiers": {(DOMAIN, device)}}

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
