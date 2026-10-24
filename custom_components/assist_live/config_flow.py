"""Refuse setup until the integration has a runtime implementation."""

from homeassistant import config_entries


class AssistLiveConfigFlow(config_entries.ConfigFlow, domain="assist_live"):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> config_entries.ConfigFlowResult:
        return self.async_abort(reason="bootstrap_only")
