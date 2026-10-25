from homeassistant import config_entries


class ConfigFlow(config_entries.ConfigFlow, domain="assist_live_test_agent"):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id("assist_live_test_agent")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Assist live test agent", data={})
