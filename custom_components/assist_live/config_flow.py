from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import probatio as vol
from assist_live_client import AssistLiveClient
from assist_live_client.protocol import ProtocolError
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_integration
from yarl import URL

from .const import (
    CONF_ENDPOINT_GENERATION,
    CONF_FALLBACK_TTS_VOICE,
    CONF_NOTIFY_TTS_FAILURE,
    DOMAIN,
)
from .settings import effective_settings


def request_key(url: str) -> tuple[str, str, int | None, str] | None:
    """Give the server request that the duplicate check compares.

    The pinned client removes a trailing "/" from the URL text, adds the API
    path, and then parses the full URL with yarl (client.py:55 and :94).
    This key uses the same steps for the bindings request of the flow. Thus
    two URLs are equal only if the client sends the request to the same
    scheme, encoded host, port, and path. The case of the scheme and host changes
    only where yarl makes it lowercase. The port is the port of the
    connection, so an explicit default port gives the same key.
    """
    try:
        submitted = URL(url)
        request = URL(url.rstrip("/") + "/v1/bindings")
    except ValueError:
        return None
    # The client refuses these URLs (client.py:47-53), and the flow then
    # reports invalid_url. Thus the duplicate check does not compare them.
    if (
        submitted.scheme not in {"http", "https"}
        or submitted.user
        or submitted.query
        or submitted.fragment
    ):
        return None
    # The key uses the encoded host, as the aiohttp connector does. Reading the
    # decoded host can raise UnicodeError for a malformed international name.
    if not request.raw_host:
        return None
    return (request.scheme, request.raw_host, request.port, request.raw_path)


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
    selected = {settings[field] for field in ("fallback_stt", "fallback_tts")}
    decisions = {
        entity_id: decision
        for entity_id, decision in settings.get("local_decisions", {}).items()
        if entity_id in selected
    }
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
    if not errors:
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


def fallback_errors(hass: HomeAssistant, settings: dict[str, Any]) -> dict[str, str]:
    from homeassistant.components import stt

    from .tts import fallback_engine

    errors = {}
    for field in ("fallback_stt", "fallback_tts"):
        entity_id = settings[field]
        if is_forwarder(hass, entity_id):
            errors[field] = "fallback_loop"
        elif field == "fallback_stt":
            try:
                fallback = stt.async_get_speech_to_text_entity(hass, entity_id)
            except KeyError:
                fallback = None
            if fallback is None:
                errors[field] = "invalid_fallback_entity"
        else:
            try:
                fallback_engine(hass, entity_id)
            except HomeAssistantError, KeyError:
                errors[field] = "invalid_fallback_entity"
    return errors


def fallback_voices(hass: HomeAssistant, entity_id: str) -> dict[str, str]:
    from .tts import fallback_engine

    voices = {"": "Default"}
    try:
        fallback = fallback_engine(hass, entity_id)
    except HomeAssistantError, KeyError:
        return voices
    for language in fallback.supported_languages:
        for voice in fallback.async_get_supported_voices(language) or []:
            voices[voice.voice_id] = voice.name
    return voices


def fallback_voice_schema(
    settings: dict[str, Any], voices: dict[str, str]
) -> dict[Any, Any]:
    return {
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


def valid_satellite(hass: HomeAssistant, device: str, pipeline_select: str) -> bool:
    registry = er.async_get(hass)
    selected = registry.async_get(pipeline_select)
    return bool(
        selected
        and selected.domain == "select"
        and selected.device_id == device
        and selected.translation_key in {"pipeline", "pipeline_n"}
        and any(
            e.domain == "assist_satellite" and e.device_id == device
            for e in registry.entities.values()
        )
    )


def _bindings_match(entry: config_entries.ConfigEntry, bindings: set[str]) -> bool:
    configured = set(entry.data["satellites"].values())
    return bool(configured) and configured <= bindings


def _bindings_error(error: ProtocolError) -> str:
    return "invalid_auth" if error.kind == "unauthorized" else "cannot_connect"


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
        self._reconfigure_url: str | None = None

    def _abort_if_configured(
        self, url: object, token: object, *, except_entry_id: str | None = None
    ) -> None:
        # The connection slots of one server token are sufficient for five
        # satellites. Thus two entries can use one server with different
        # tokens. An entry with the same server and token is a duplicate.
        # Entries from before this check keep the URL as the user wrote it.
        if not isinstance(url, str) or not isinstance(token, str):
            return
        if (key := request_key(url)) is None:
            return
        for entry in self._async_current_entries(include_ignore=False):
            stored = entry.data.get("url")
            if (
                entry.entry_id != except_entry_id
                and isinstance(stored, str)
                and entry.data.get("token") == token
                and request_key(stored) == key
            ):
                raise AbortFlow("already_configured")

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors = {}
        if user_input:
            self._abort_if_configured(user_input["url"], user_input["token"])
            if errors := fallback_errors(self.hass, user_input):
                pass
            elif errors := await mode_errors(self.hass, user_input):
                pass
            else:
                client = None
                try:
                    try:
                        client = AssistLiveClient(
                            user_input["url"],
                            user_input["token"],
                            session=async_get_clientsession(self.hass),
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
                except ProtocolError as error:
                    errors["base"] = _bindings_error(error)
                except ValueError:
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
            if self._reconfigure_url is not None:
                url, self._reconfigure_url = self._reconfigure_url, None
                self._http_confirmed = url
                return await self.async_step_reconfigure({"url": url})
            self._http_confirmed = self.settings["url"]
            return await self.async_step_user(self.settings)
        return self.async_show_form(step_id="http_warning", data_schema=vol.Schema({}))

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url, token = entry.data["url"], user_input["token"]
            try:
                self._abort_if_configured(url, token, except_entry_id=entry.entry_id)
                client = AssistLiveClient(
                    url, token, session=async_get_clientsession(self.hass)
                )
                try:
                    bindings = {row["satellite"] for row in await client.bindings()}
                finally:
                    await client.close()
                if entry.data["url"] != url:
                    errors["base"] = "server_changed"
                elif not _bindings_match(entry, bindings):
                    errors["base"] = "wrong_server"
                else:
                    self._abort_if_configured(
                        url, token, except_entry_id=entry.entry_id
                    )
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, "token": token}
                    )
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")
            except AbortFlow:
                errors["base"] = "token_in_use"
            except ProtocolError as error:
                errors["base"] = _bindings_error(error)
            except ValueError:
                errors["base"] = "invalid_url"
        return self.async_show_form(
            step_id="reauth_confirm",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("token"): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    )
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        url = entry.data["url"]
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input["url"]
            if (key := request_key(url)) is None:
                errors["base"] = "invalid_url"
            elif key[0] == "http" and url != self._http_confirmed:
                self._reconfigure_url = url
                return await self.async_step_http_warning()
            else:
                token = entry.data["token"]
                self._abort_if_configured(url, token, except_entry_id=entry.entry_id)
                client = AssistLiveClient(
                    url, token, session=async_get_clientsession(self.hass)
                )
                try:
                    bindings = {row["satellite"] for row in await client.bindings()}
                except ProtocolError as error:
                    errors["base"] = _bindings_error(error)
                finally:
                    await client.close()
                if not errors:
                    if entry.data["token"] != token:
                        errors["base"] = "server_changed"
                    elif not _bindings_match(entry, bindings):
                        errors["base"] = "wrong_server"
                    else:
                        self._abort_if_configured(
                            url, token, except_entry_id=entry.entry_id
                        )
                        url = url.rstrip("/")
                        data = {**entry.data, "url": url}
                        if key != request_key(entry.data["url"]):
                            data[CONF_ENDPOINT_GENERATION] = (
                                entry.data.get(CONF_ENDPOINT_GENERATION, 0) + 1
                            )
                        if self.hass.config_entries.async_update_entry(
                            entry, data=data
                        ):
                            self.hass.config_entries.async_schedule_reload(
                                entry.entry_id
                            )
                        return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required("url"): str}), {"url": url}
            ),
            errors=errors,
        )

    async def async_step_satellite(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors = {}
        if user_input:
            # A different flow can make an entry for the same server and token
            # after the user step. No await occurs between this check and
            # async_create_entry, so a second flow cannot get through it.
            self._abort_if_configured(
                self.settings.get("url"), self.settings.get("token")
            )
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
            else:
                self.satellites[device] = satellite
                self.selects[device] = user_input["pipeline_select"]
                if len(self.satellites) == 5 or not user_input.get(
                    "add_another", False
                ):
                    return self.async_create_entry(
                        title="assist-live",
                        data={
                            **self.settings,
                            # The client makes only this change to the URL.
                            "url": self.settings["url"].rstrip("/"),
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
        self.settings = effective_settings(entry)
        self._confirm_fields: list[str] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return await self._step_init(user_input, confirmed=False)

    async def _step_init(
        self, user_input: dict[str, Any] | None, *, confirmed: bool
    ) -> config_entries.ConfigFlowResult:
        settings = self.settings
        errors = {}
        if user_input is not None:
            previous = settings.copy()
            saved = effective_settings(self.entry)
            settings.update(user_input)
            reconfirm = []
            if not confirmed:
                for field in ("fallback_stt", "fallback_tts"):
                    checkbox = f"{field}_local"
                    if settings.get(checkbox) and any(
                        source.get(checkbox) and source[field] != settings[field]
                        for source in (saved, previous)
                    ):
                        settings[checkbox] = False
                        reconfirm.append(field)
            if previous["fallback_tts"] != settings["fallback_tts"]:
                settings[CONF_FALLBACK_TTS_VOICE] = ""
            errors = fallback_errors(self.hass, settings)
            if not errors:
                self._confirm_fields = [
                    f"{field}_local"
                    for field in reconfirm
                    if destination(await entity_iot_class(self.hass, settings[field]))
                    == "unknown"
                ]
                if self._confirm_fields:
                    return await self.async_step_confirm_local()
                errors = {
                    f"{field}_local": error
                    for field, error in (await mode_errors(self.hass, settings)).items()
                }
            if not errors:
                if settings["fallback_tts"] != saved["fallback_tts"]:
                    return await self.async_step_fallback_voice()
                if settings.get(CONF_FALLBACK_TTS_VOICE, "") not in fallback_voices(
                    self.hass, settings["fallback_tts"]
                ):
                    errors[CONF_FALLBACK_TTS_VOICE] = "invalid_fallback_voice"
                else:
                    return self._finish()
        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "fallback_stt", default=settings["fallback_stt"]
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="stt")
                    ),
                    vol.Required(
                        "fallback_tts", default=settings["fallback_tts"]
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="tts")
                    ),
                    **mode_schema(settings),
                    **fallback_voice_schema(
                        settings, fallback_voices(self.hass, settings["fallback_tts"])
                    ),
                }
            ),
        )

    async def async_step_confirm_local(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return await self._step_init(user_input, confirmed=True)
        return self.async_show_form(
            step_id="confirm_local",
            data_schema=vol.Schema(
                {
                    vol.Optional(field, default=False): bool
                    for field in self._confirm_fields
                }
            ),
        )

    async def async_step_fallback_voice(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        settings = self.settings
        voices = fallback_voices(self.hass, settings["fallback_tts"])
        errors = {}
        if user_input is not None:
            settings.update(user_input)
            if fallback_errors(self.hass, settings) or await mode_errors(
                self.hass, settings
            ):
                return await self._step_init(settings, confirmed=True)
            if settings.get(CONF_FALLBACK_TTS_VOICE, "") not in voices:
                errors[CONF_FALLBACK_TTS_VOICE] = "invalid_fallback_voice"
            else:
                return self._finish()
        return self.async_show_form(
            step_id="fallback_voice",
            errors=errors,
            data_schema=vol.Schema(fallback_voice_schema(settings, voices)),
        )

    def _finish(self) -> config_entries.ConfigFlowResult:
        settings = self.settings
        return self.async_create_entry(
            title="",
            data={
                **self.entry.options,
                **{
                    key: settings.get(key, False)
                    for key in (
                        "local_only",
                        "record_transcripts",
                        "fallback_stt_local",
                        "fallback_tts_local",
                    )
                },
                "fallback_stt": settings["fallback_stt"],
                "fallback_tts": settings["fallback_tts"],
                "local_decisions": settings["local_decisions"],
                CONF_FALLBACK_TTS_VOICE: settings.get(CONF_FALLBACK_TTS_VOICE, ""),
            },
        )
