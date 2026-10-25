from homeassistant.helpers import issue_registry as ir


def test_shared_pipeline_and_unavailable_server(hass):
    from custom_components.assist_live.repairs import update_issues

    update_issues(hass, "entry", False, ["shared", "shared"], {"shared"})
    registry = ir.async_get(hass)
    assert registry.async_get_issue("assist_live", "entry_server_unavailable")
    assert registry.async_get_issue("assist_live", "entry_shared_pipeline")
    update_issues(hass, "entry", True, ["one", "two"], {"one", "two"})
    assert registry.async_get_issue("assist_live", "entry_server_unavailable") is None
    assert registry.async_get_issue("assist_live", "entry_shared_pipeline") is None
    update_issues(hass, "entry", True, ["standard", "standard"], {"one"})
    assert registry.async_get_issue("assist_live", "entry_shared_pipeline") is None


async def test_manually_created_forwarding_pipeline_is_checked(hass, client):
    from types import SimpleNamespace
    from unittest.mock import patch

    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime

    engine = er.async_get(hass).async_get_or_create("stt", "assist_live", "satellite-a")
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "satellites": {"device-a": "satellite-a", "device-b": "satellite-b"},
            "pipeline_selects": {
                "device-a": "select.pipeline_a",
                "device-b": "select.pipeline_b",
            },
        },
    )
    runtime = Runtime(hass, entry, client)
    hass.states.async_set("select.pipeline_a", "Manual forwarding")
    hass.states.async_set("select.pipeline_b", "Manual forwarding")
    pipeline = SimpleNamespace(
        id="manual",
        name="Manual forwarding",
        stt_engine=engine.entity_id,
        tts_engine=None,
    )
    with patch(
        "homeassistant.components.assist_pipeline.async_get_pipelines",
        return_value=[pipeline],
    ):
        await runtime._maintenance(None)
    assert ir.async_get(hass).async_get_issue(
        "assist_live", f"{entry.entry_id}_shared_pipeline"
    )
