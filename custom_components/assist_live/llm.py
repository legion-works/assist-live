from homeassistant.components.llm import LLMTools
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.llm import LLM_API_ASSIST, LLMContext

from .const import DOMAIN

SPOKEN_REPLY_PROMPT = (
    "The satellite speaks your reply aloud. "
    "Use plain spoken sentences only. "
    "Do not use Markdown marks, lists, headings, tables, web addresses, "
    "code, or emoji. "
    "If you must get an answer from the user, end the reply with one question. "
    "End that question with a question mark (?)."
)


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: LLMContext, api_id: str
) -> LLMTools | None:
    if api_id != LLM_API_ASSIST or not (device_id := llm_context.device_id):
        return None
    if dr.async_get(hass).async_get(device_id) is None:
        return None
    if not any(
        entity.domain == "assist_satellite"
        for entity in er.async_entries_for_device(er.async_get(hass), device_id)
    ):
        return None
    if not any(
        entry.state is ConfigEntryState.LOADED
        and device_id in entry.data.get("satellites", {})
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        return None
    return LLMTools(tools=[], prompt=SPOKEN_REPLY_PROMPT)
