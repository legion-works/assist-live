from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import probatio as vol
from assist_live_client import AssistLiveClient
from assist_live_client.protocol import ProtocolError
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.loader import async_get_integration

from .const import CONF_FALLBACK_TTS_VOICE, CONF_NOTIFY_TTS_FAILURE, DOMAIN


def destination(iot_class: str | None) -> str:
    if isinstance(iot_class, str) and iot_class.startswith("local"):
        return "local"
    if isinstance(iot_class, str) and iot_class.startswith("cloud"):
        return "cloud"
    return "unknown"


async def entity_iot_class(hass: HomeAssistant, entity_id: str) -> str | None:
    registered = er.async_get(hass).async_get(entity_id)
    if registered is None:
        return None
    try:
        integration = await async_get_integration(hass, registered.platform)
    except HomeAssistantError:
        return None
    value = integration.manifest.get("iot_class")
    return value if isinstance(value, str) else None


async def mode_errors(hass: HomeAssistant, settings: dict[str, Any]) -> dict[str, str]:
    errors = {}
    decisions = dict(settings.get("local_decisions", {}))
    for field in ("fallback_stt", "fallback_tts"):
        entity_id = settings[field]
        mode = destination(await entity_iot_class(hass, entity_id))
        choice = settings.get(f"{field}_local", False)
        if choice and mode == "unknown":
            decisions[entity_id] = decisions.get(entity_id) or {
                "destination": "local",
                "recorded_at": datetime.now(UTC).isoformat(),
            }
        else:
            decisions.pop(entity_id, None)
        if settings.get("local_only", False):
            if mode == "cloud":
                errors[field] = "cloud_destination"
            elif mode == "unknown" and entity_id not in decisions:
                errors[field] = "unknown_destination"
    settings["local_decisions"] = decisions
    return errors


def mode_schema(settings: dict[str, Any]) -> dict[Any, Any]:
    return {
        vol.Optional("local_only", default=settings.get("local_only", False)): bool,
        vol.Optional(
            "record_transcripts", default=settings.get("record_transcripts", False)
        ): bool,
        vol.Optional(
            "fallback_stt_local", default=settings.get("fallback_stt_local", False)
        ): bool,
        vol.Optional(
            "fallback_tts_local", default=settings.get("fallback_tts_local", False)
        ): bool,
    }


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

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return AssistLiveOptionsFlow(config_entry)

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
            elif errors := await mode_errors(self.hass, user_input):
                pass
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
                    **mode_schema({}),
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


class AssistLiveOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        from .tts import fallback_engine

        settings = {**self.entry.data, **self.entry.options}
        voices = {"": "Default"}
        try:
            fallback = fallback_engine(self.hass, settings["fallback_tts"])
        except HomeAssistantError, KeyError:
            pass
        else:
            for language in fallback.supported_languages:
                for voice in fallback.async_get_supported_voices(language) or []:
                    voices[voice.voice_id] = voice.name
        errors = {}
        if user_input is not None:
            settings.update(user_input)
            errors = {
                f"{field}_local": error
                for field, error in (await mode_errors(self.hass, settings)).items()
            }
            if settings.get(CONF_FALLBACK_TTS_VOICE, "") not in voices:
                errors[CONF_FALLBACK_TTS_VOICE] = "invalid_fallback_voice"
            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        key: settings.get(key, False)
                        for key in (
                            "local_only",
                            "record_transcripts",
                            "fallback_stt_local",
                            "fallback_tts_local",
                        )
                    }
                    | {
                        "local_decisions": settings["local_decisions"],
                        CONF_FALLBACK_TTS_VOICE: settings.get(
                            CONF_FALLBACK_TTS_VOICE, ""
                        ),
                    },
                )
        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    **mode_schema(settings),
                    vol.Optional(
                        CONF_FALLBACK_TTS_VOICE,
                        default=settings.get(CONF_FALLBACK_TTS_VOICE, ""),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=value, label=label)
                                for value, label in voices.items()
                            ]
                        )
                    ),
                }
            ),
        )
