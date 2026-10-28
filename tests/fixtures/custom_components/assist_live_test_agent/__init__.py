from contextvars import ContextVar
from time import monotonic_ns

from homeassistant.components.intent import async_register_timer_handler
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

ACTIVE_RECORD = ContextVar("assist_live_test_record", default=None)
DEVICE_ID = "assist_live_virtual"


def register_timer(hass, device_id=DEVICE_ID):
    @callback
    def timer_event(event_type, timer):
        if (record := ACTIVE_RECORD.get()) is not None:
            stamp = monotonic_ns()
            record["actions"].append(
                {"kind": "timer", "event": event_type.value, "seconds": timer.seconds}
            )
            record["operations"].append(
                {
                    "service": "timer." + event_type.value,
                    "target": DEVICE_ID,
                    "arguments": {"seconds": timer.seconds},
                    "t_monotonic_ns": stamp,
                }
            )

    return async_register_timer_handler(hass, device_id, timer_event)


async def async_setup(hass, config):
    return True


async def async_setup_entry(hass, entry):
    area = ar.async_get(hass).async_get_or_create("Virtual test area")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("assist_live_test_agent", DEVICE_ID)},
        name="Virtual timer receiver",
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    entry.runtime_data = device.id
    entry.async_on_unload(register_timer(hass, device.id))
    await hass.config_entries.async_forward_entry_setups(entry, ["conversation"])
    return True


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, ["conversation"])
