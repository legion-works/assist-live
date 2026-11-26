from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class AssistLiveEntity(Entity):
    """Base of the assist-live entities of one satellite device."""

    _attr_has_entity_name = True

    def _set_device(self, device: str, satellite: str) -> None:
        self._satellite_device_id = device
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, satellite)}, name=satellite
        )

    def _attach_device(self, hass: HomeAssistant) -> None:
        if registered := dr.async_get(hass).async_get(
            self._satellite_device_id, include_composite_devices=False
        ):
            # Copying device info registers a device under this config entry.
            self._attr_device_info = None
            self.device_entry = registered
