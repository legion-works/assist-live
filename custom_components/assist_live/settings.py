from typing import Any

from homeassistant.config_entries import ConfigEntry


def effective_settings(entry: ConfigEntry) -> dict[str, Any]:
    return {**entry.data, **entry.options}
