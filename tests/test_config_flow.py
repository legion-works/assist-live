from unittest.mock import AsyncMock, patch

import pytest
from test_pipeline_contract import pipeline_hass as configured_hass  # noqa: F401


async def test_flow_refuses_fallback_loop(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    with patch(
        "custom_components.assist_live.config_flow.is_forwarder", return_value=True
    ):
        result = await flow.async_step_user(
            {
                "url": "http://server.example",
                "token": "test-only",
                "fallback_stt": "stt.forward",
                "fallback_tts": "tts.standard",
            }
        )
    assert result["errors"] == {"base": "fallback_loop"}


async def test_provisioned_unique_device_bindings(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.bindings = {"satellite-a", "satellite-b"}
    flow.settings = {}
    with patch(
        "custom_components.assist_live.config_flow.valid_satellite", return_value=True
    ):
        result = await flow.async_step_satellite(
            {
                "device": "device-a",
                "satellite": "satellite-a",
                "pipeline_select": "select.pipeline_a",
                "add_another": True,
            }
        )
        assert result["step_id"] == "satellite"
        result = await flow.async_step_satellite(
            {
                "device": "device-b",
                "satellite": "satellite-a",
                "pipeline_select": "select.pipeline_b",
                "add_another": True,
            }
        )
        assert result["errors"] == {"base": "binding_used"}
        result = await flow.async_step_satellite(
            {
                "device": "device-b",
                "satellite": "unknown",
                "pipeline_select": "select.pipeline_b",
                "add_another": True,
            }
        )
        assert result["errors"] == {"base": "invalid_satellite"}


async def test_user_validates_server_and_advances(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    client = AsyncMock()
    client.bindings.return_value = [{"satellite": "satellite-a"}]
    with (
        patch(
            "custom_components.assist_live.config_flow.is_forwarder", return_value=False
        ),
        patch(
            "custom_components.assist_live.config_flow.AssistLiveClient",
            return_value=client,
        ),
    ):
        result = await flow.async_step_user(
            {
                "url": "http://server.example",
                "token": "test-only",
                "fallback_stt": "stt.standard",
                "fallback_tts": "tts.standard",
            }
        )
    assert result["step_id"] == "satellite"
    assert flow.bindings == {"satellite-a"}
    client.close.assert_awaited_once()


async def test_binding_cannot_be_reused_by_another_entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    existing = MockConfigEntry(
        domain="assist_live", data={"satellites": {"device-a": "satellite-a"}}
    )
    existing.add_to_hass(hass)
    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.bindings = {"satellite-a"}
    with patch(
        "custom_components.assist_live.config_flow.valid_satellite", return_value=True
    ):
        result = await flow.async_step_satellite(
            {
                "device": "device-b",
                "satellite": "satellite-a",
                "pipeline_select": "select.pipeline_b",
                "add_another": False,
            }
        )
    assert result["errors"] == {"base": "binding_used"}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_runtime_subscribes_each_key_before_first_turn(hass, client):
    import asyncio

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime

    entered = {key: asyncio.Event() for key in ("satellite-a", "satellite-b")}
    queues = {key: asyncio.Queue() for key in entered}

    async def events(key, after_event_id=0):
        entered[key].set()
        while True:
            yield await queues[key].get()
            queues[key].task_done()

    client.events = events
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "satellites": {"device-a": "satellite-a", "device-b": "satellite-b"},
            "pipeline_selects": {},
        },
    )
    runtime = Runtime(hass, entry, client)
    await runtime.start()
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in entered.values())), 1
        )
        a = runtime.ledger.open_voice("satellite-a")
        b = runtime.ledger.open_voice("satellite-b")
        await queues["satellite-a"].put(
            {
                "event_id": 1,
                "event": {
                    "satellite": "satellite-a",
                    "turn_id": a.turn_id,
                    "stage": "playback_end",
                },
            }
        )
        await queues["satellite-a"].join()
        assert runtime.ledger.current("satellite-a") is None
        assert runtime.ledger.current("satellite-b") == b
    finally:
        await runtime.stop()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_real_entry_registers_pair_and_owns_pipeline(configured_hass, client):  # noqa: F811
    import asyncio

    from homeassistant.components import assist_pipeline
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    hass = configured_hass

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events
    client.bindings.return_value = [{"satellite": "satellite-a"}]
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "url": "http://server.example",
            "token": "test-only",
            "fallback_stt": "stt.contract_stt",
            "fallback_tts": "tts.contract_tts",
            "satellites": {"device-a": "satellite-a"},
            "pipeline_selects": {},
            "voices": {"device-a": "voice-two"},
        },
    )
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "satellite-a")}
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "satellites": {device.id: "satellite-a"},
            "voices": {device.id: "voice-two"},
        },
    )
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
    runtime = hass.data["assist_live"][entry.entry_id]
    stt_id = runtime.stt[device.id].entity_id
    tts_id = runtime.tts[device.id].entity_id
    assert hass.states.get(stt_id) is not None
    assert hass.states.get(tts_id) is not None
    assert er.async_get(hass).async_get(stt_id).device_id == device.id
    assert er.async_get(hass).async_get(tts_id).device_id == device.id
    saved = entry.data["pipelines"][device.id]
    pipeline = next(
        p for p in assist_pipeline.async_get_pipelines(hass) if p.id == saved
    )
    assert pipeline.stt_engine == stt_id
    assert pipeline.tts_engine == tts_id
    assert pipeline.tts_voice == "voice-two"
    assert await hass.config_entries.async_unload(entry.entry_id)
    before = {p.id for p in assist_pipeline.async_get_pipelines(hass)}
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert {p.id for p in assist_pipeline.async_get_pipelines(hass)} == before
    assert entry.data["pipelines"][device.id] == saved
    assert await hass.config_entries.async_unload(entry.entry_id)
