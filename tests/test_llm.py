import inspect
from dataclasses import fields
from typing import get_type_hints

import pytest
from homeassistant.components import llm as llm_component
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, is_callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_pipeline_contract import pipeline_hass  # noqa: F401

DOMAIN = "assist_live"
PROMPT_START = "The satellite speaks your reply aloud."
QUESTION_RULE = (
    "If you must get an answer from the user, end the reply with one question. "
    "End that question with a question mark (?)."
)


def context(device_id):
    return llm.LLMContext(
        platform="test",
        context=None,
        language="en",
        assistant="conversation",
        device_id=device_id,
    )


@pytest.fixture
async def llm_hass(pipeline_hass, enable_custom_integrations):  # noqa: F811
    hass = pipeline_hass
    assert await async_setup_component(hass, DOMAIN, {})
    assert await async_setup_component(hass, "llm", {})
    yield hass
    for entry in hass.config_entries.async_entries(DOMAIN):
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


def satellite(hass, index, *, loaded=True, registered=True):
    entry = MockConfigEntry(domain=DOMAIN, data={"satellites": {}})
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", f"satellite-{index}")}
    )
    if registered:
        er.async_get(hass).async_get_or_create(
            "assist_satellite", "test", f"satellite-{index}", device_id=device.id
        )
    hass.config_entries.async_update_entry(
        entry, data={"satellites": {device.id: f"satellite-{index}"}}
    )
    entry.mock_state(
        hass, ConfigEntryState.LOADED if loaded else ConfigEntryState.NOT_LOADED
    )
    return entry, device.id


async def test_assist_api_discovers_prompt_without_llm_dependency(llm_hass):
    hass = llm_hass
    integration = await async_get_integration(hass, DOMAIN)
    assert "llm" not in integration.dependencies
    assert "llm" not in integration.manifest.get("after_dependencies", [])
    _, device_id = satellite(hass, 1)
    api = await llm.async_get_api(hass, "assist", context(device_id))
    assert PROMPT_START in api.api_prompt, "satellite_prompt_missing"
    assert QUESTION_RULE in api.api_prompt, "question_rule_missing"
    for rule in (
        "Use plain spoken sentences only.",
        "Do not use Markdown marks, lists, headings, tables, web addresses, "
        "code, or emoji.",
    ):
        assert rule in api.api_prompt, "spoken_sentence_rule_missing"
    from custom_components.assist_live import llm as platform

    assert (await integration.async_get_platform("llm")) is platform
    result = platform.async_get_tools(hass, context(device_id), "assist")
    assert isinstance(result, llm_component.LLMTools)
    assert result.tools == [], "platform_added_tools"
    assert result.prompt == platform.SPOKEN_REPLY_PROMPT


@pytest.mark.parametrize(
    "device_kind", ["other", "missing", "stale", "unknown", "unassigned"]
)
async def test_assist_api_excludes_other_devices(llm_hass, device_kind):
    hass = llm_hass
    _, device_id = satellite(hass, 1)
    if device_kind in ("other", "unassigned"):
        other = MockConfigEntry(domain="test")
        other.add_to_hass(hass)
        device_id = (
            dr.async_get(hass)
            .async_get_or_create(
                config_entry_id=other.entry_id, identifiers={("test", "other")}
            )
            .id
        )
        if device_kind == "unassigned":
            er.async_get(hass).async_get_or_create(
                "assist_satellite", "test", "unassigned", device_id=device_id
            )
    elif device_kind == "missing":
        device_id = None
    elif device_kind == "stale":
        _, device_id = satellite(hass, 2, registered=False)
    else:
        device_id = "unknown-device"
    api = await llm.async_get_api(hass, "assist", context(device_id))
    assert PROMPT_START not in api.api_prompt, "non_satellite_prompt_leak"
    from custom_components.assist_live import llm as platform

    assert platform.async_get_tools(hass, context(device_id), "assist") is None


async def test_assist_api_checks_each_entry_load_state(llm_hass):
    hass = llm_hass
    first, first_device = satellite(hass, 1)
    second, second_device = satellite(hass, 2)
    for device_id in (first_device, second_device):
        api = await llm.async_get_api(hass, "assist", context(device_id))
        assert PROMPT_START in api.api_prompt, "loaded_entry_prompt_missing"
    second.mock_state(hass, ConfigEntryState.NOT_LOADED)
    api = await llm.async_get_api(hass, "assist", context(second_device))
    assert PROMPT_START not in api.api_prompt, "unloaded_entry_prompt_leak"
    from custom_components.assist_live import llm as platform

    assert platform.async_get_tools(hass, context(second_device), "assist") is None
    api = await llm.async_get_api(hass, "assist", context(first_device))
    assert PROMPT_START in api.api_prompt, "other_loaded_entry_prompt_missing"
    first.mock_state(hass, ConfigEntryState.NOT_LOADED)
    api = await llm.async_get_api(hass, "assist", context(first_device))
    assert PROMPT_START not in api.api_prompt, "last_unloaded_entry_prompt_leak"


async def test_other_api_gets_no_prompt(llm_hass):
    hass = llm_hass
    _, device_id = satellite(hass, 1)

    class OtherAPI(llm.API):
        async def async_get_api_instance(self, llm_context):
            result = await llm_component.async_get_tools(hass, llm_context, self.id)
            return llm.APIInstance(
                api=self,
                api_prompt=result.prompt or "",
                llm_context=llm_context,
                tools=result.tools,
            )

    llm.async_register_api(hass, OtherAPI(hass=hass, id="other", name="Other"))
    api = await llm.async_get_api(hass, "other", context(device_id))
    assert PROMPT_START not in api.api_prompt, "other_api_prompt_leak"
    from custom_components.assist_live import llm as platform

    assert platform.async_get_tools(hass, context(device_id), "other") is None


def test_llm_platform_public_contract():
    from custom_components.assist_live import llm as platform

    assert [field.name for field in fields(llm_component.LLMTools)] == [
        "tools",
        "prompt",
    ]
    assert get_type_hints(llm_component.LLMTools) == {
        "tools": list[llm.Tool],
        "prompt": str | None,
    }
    protocol = llm_component.LLMToolsPlatformProtocol.async_get_tools
    assert list(inspect.signature(protocol).parameters) == [
        "self",
        "hass",
        "llm_context",
        "api_id",
    ]
    assert (
        get_type_hints(protocol)
        == get_type_hints(platform.async_get_tools)
        == {
            "hass": HomeAssistant,
            "llm_context": llm.LLMContext,
            "api_id": str,
            "return": llm_component.LLMTools | None,
        }
    )
    assert is_callback(platform.async_get_tools)
    assert not inspect.iscoroutinefunction(platform.async_get_tools)
