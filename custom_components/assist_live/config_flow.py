from typing import Any
from urllib.parse import urlsplit

import probatio as vol
from assist_live_client import AssistLiveClient
from assist_live_client.protocol import ProtocolError
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import CONF_NOTIFY_TTS_FAILURE, DOMAIN


def is_forwarder(hass: HomeAssistant, entity_id: str) -> bool:
    entity = er.async_get(hass).async_get(entity_id)
    return entity is not None and entity.platform == DOMAIN


def valid_satellite(hass: HomeAssistant, device: str, pipeline_select: str) -> bool:
    registry = er.async_get(hass)
    selected = registry.async_get(pipeline_select)
    return bool(
        selected
        and selected.domain == "select"
        and selected.device_id == device
        and any(
            e.domain == "assist_satellite" and e.device_id == device
            for e in registry.entities.values()
        )
    )


class AssistLiveConfigFlow(config_entries.ConfigFlow, domain="assist_live"):
    VERSION = 1

    def __init__(self) -> None:
        self.settings: dict[str, Any] = {}
        self.bindings: set[str] = set()
        self.satellites: dict[str, str] = {}
        self.selects: dict[str, str] = {}
        self._http_confirmed: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors = {}
        if user_input:
            if any(
                is_forwarder(self.hass, user_input[field])
                for field in ("fallback_stt", "fallback_tts")
            ):
                errors["base"] = "fallback_loop"
            else:
                client = None
                try:
                    try:
                        client = AssistLiveClient(
                            user_input["url"], user_input["token"]
                        )
                    except ValueError:
                        errors["base"] = "invalid_url"
                    else:
                        self.settings = user_input
                        if (
                            urlsplit(user_input["url"]).scheme == "http"
                            and self._http_confirmed != user_input["url"]
                        ):
                            return await self.async_step_http_warning()
                        self.bindings = {
                            row["satellite"] for row in await client.bindings()
                        }
                        if not self.bindings:
                            raise ValueError("no bindings")
                        return await self.async_step_satellite()
                except ProtocolError, ValueError:
                    errors["base"] = "cannot_connect"
                finally:
                    if client is not None:
                        await client.close()
        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("url"): str,
                    vol.Required("token"): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                    vol.Required("fallback_stt"): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="stt")
                    ),
                    vol.Required("fallback_tts"): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="tts")
                    ),
                    vol.Optional(CONF_NOTIFY_TTS_FAILURE, default=False): bool,
                }
            ),
        )

    async def async_step_http_warning(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._http_confirmed = self.settings["url"]
            return await self.async_step_user(self.settings)
        return self.async_show_form(step_id="http_warning", data_schema=vol.Schema({}))

    async def async_step_satellite(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors = {}
        if user_input:
            device, satellite = user_input["device"], user_input["satellite"]
            existing_bindings = [
                entry.data.get("satellites", {})
                for entry in self.hass.config_entries.async_entries(DOMAIN)
            ]
            if satellite not in self.bindings or not valid_satellite(
                self.hass, device, user_input["pipeline_select"]
            ):
                errors["base"] = "invalid_satellite"
            elif (
                device in self.satellites
                or satellite in self.satellites.values()
                or any(
                    device in bindings or satellite in bindings.values()
                    for bindings in existing_bindings
                )
            ):
                errors["base"] = "binding_used"
            elif len(self.satellites) >= 5:
                errors["base"] = "connection_limit"
            else:
                self.satellites[device] = satellite
                self.selects[device] = user_input["pipeline_select"]
                if not user_input.get("add_another", False):
                    return self.async_create_entry(
                        title="assist-live",
                        data={
                            **self.settings,
                            "satellites": self.satellites,
                            "pipeline_selects": self.selects,
                        },
                    )
        return self.async_show_form(
            step_id="satellite",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("device"): selector.DeviceSelector(),
                    vol.Required("satellite"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=sorted(self.bindings))
                    ),
                    vol.Required("pipeline_select"): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="select")
                    ),
                    vol.Optional("add_another", default=False): bool,
                }
            ),
        )
