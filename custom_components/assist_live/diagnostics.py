from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .tts import AssistLiveTTS


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    runtime = hass.data[DOMAIN][entry.entry_id]
    streaming = full_file = 0
    for entity in runtime.tts.values():
        if isinstance(entity, AssistLiveTTS):
            if entity.fallback.async_supports_streaming_input():
                streaming += 1
            else:
                full_file += 1
    return {
        "protocol_version": 1,
        "tts_fallback": {"streaming": streaming, "full_file": full_file},
        "failures": dict(runtime.ledger.failures),
        "late_server_failures": runtime.ledger.late_server_failures,
    }
