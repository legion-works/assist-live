from typing import Any, NotRequired, TypedDict

from homeassistant.config_entries import ConfigEntry


class FallbackMetadata(TypedDict):
    entity_id: str
    languages: list[str]
    default_language: NotRequired[str]
    default_options: NotRequired[dict[str, str]]


def effective_settings(entry: ConfigEntry) -> dict[str, Any]:
    return {**entry.data, **entry.options}
