from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class AssistLiveEntity(Entity):
    """Base of the assist-live entities of one satellite device."""

    _attr_has_entity_name = True

    def _set_device(self, device: str, satellite: str) -> None:
        # This device data applies only if the satellite device is not in the
        # registry. The name keeps the entity names different for each key.
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, device)}, name=satellite
        )

    def _attach_device(self, hass: HomeAssistant, device: str) -> None:
        # The entities attach to the satellite device of a different integration.
        # Identifiers and connections select that device. The other device
        # fields stay with the integration that owns the device.
        if registered := dr.async_get(hass).async_get(device):
            self._attr_device_info = dr.DeviceInfo(
                identifiers=registered.identifiers,
                connections=registered.connections
                if isinstance(registered, dr.DeviceEntry)
                else set(),
            )
