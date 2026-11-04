from datetime import timedelta
from pathlib import Path

import pytest
from test_pipeline_contract import pipeline_hass as configured_hass  # noqa: F401


@pytest.mark.parametrize("input_index", [0, 1])
@pytest.mark.parametrize(
    "condition",
    ["fresh", "stale", "claimed", "unknown", "unloaded", "ambiguous", "text"],
)
async def test_entry_mapping_links_only_own_fresh_unclaimed_voice_turn(
    configured_hass,  # noqa: F811
    monkeypatch,
    input_index,
    condition,
):
    from homeassistant.components import conversation
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.core import Context, callback
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.setup import async_setup_component
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components

    hass = configured_hass
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(
        custom_components,
        "__path__",
        [str(root / "tests/fixtures/custom_components"), *custom_components.__path__],
    )
    assert await async_setup_component(hass, "intent", {})
    from custom_components.assist_live_test_agent.conversation import TestAgent

    satellite_entry = MockConfigEntry(domain="esphome")
    satellite_entry.add_to_hass(hass)
    entry = MockConfigEntry(
        domain="assist_live",
        state=ConfigEntryState.LOADED
        if condition != "unloaded"
        else ConfigEntryState.NOT_LOADED,
    )
    entry.add_to_hass(hass)
    devices = []
    sensors = []
    for index in range(2):
        connection = {(dr.CONNECTION_NETWORK_MAC, f"02:00:00:00:00:0{index}")}
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=satellite_entry.entry_id, connections=connection
        )
        sensor_device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, connections=connection
        )
        assert device.id != sensor_device.id, "REAL_HA_SPLIT_DEVICE"
        devices.append(device)
        sensors.append(
            er.async_get(hass).async_get_or_create(
                "sensor",
                "assist_live",
                f"sat-{index}_last_turn_latency",
                config_entry=entry,
                device_id=sensor_device.id,
            )
        )
    hass.config_entries.async_update_entry(
        entry,
        data={
            "satellites": {
                device.id: f"sat-{index}" for index, device in enumerate(devices)
            }
        },
    )
    if condition == "unknown":
        hass.config_entries.async_update_entry(entry, data={"satellites": {}})
    if condition == "ambiguous":
        other = MockConfigEntry(
            domain="assist_live",
            state=ConfigEntryState.LOADED,
            data={"satellites": {devices[input_index].id: "other"}},
        )
        other.add_to_hass(hass)
        extra = er.async_get(hass).async_get_or_create(
            "sensor", "assist_live", "other_last_turn_latency", config_entry=other
        )
        hass.states.async_set(
            extra.entity_id, "0", {"turn_type": "voice", "turn_id": "other-turn"}
        )
    agent = TestAgent("short")
    await hass.data[conversation.DATA_COMPONENT].async_add_entities([agent])
    records = []
    hass.bus.async_listen(
        "assist_live_test_record", callback(lambda event: records.append(event.data))
    )
    context = Context()
    for index, sensor in enumerate(sensors):
        hass.states.async_set(
            sensor.entity_id,
            "0",
            {
                "turn_type": "text" if condition == "text" else "voice",
                "turn_id": f"turn-{index}",
            },
            timestamp=(
                dt_util.utcnow() - timedelta(seconds=10)
                if condition == "stale"
                else dt_util.utcnow()
            ).timestamp(),
        )
    if condition == "claimed":
        hass.data["assist_live_test_turn_claims"] = {
            f"turn-{input_index}": "earlier-context"
        }
    await conversation.async_converse(
        hass,
        "test unknown command",
        None,
        context,
        language="en",
        agent_id=agent.entity_id,
        device_id=devices[input_index].id,
    )
    await hass.async_block_till_done()
    assert len(records) == 1
    assert records[0]["device_id"] == devices[input_index].id, "INPUT_DEVICE_RETAINED"
    assert records[0]["turn_id"] == (
        f"turn-{input_index}" if condition == "fresh" else None
    ), "ENTRY_MAPPING_OWN_FRESH_UNCLAIMED_TURN"


async def test_native_cancel_response_records_two_then_zero_timers(
    configured_hass,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    from homeassistant.components import conversation
    from homeassistant.core import Context
    from test_native_functions import agent_bench

    from tests.test_fault_hardware import arguments
    from tools.fault_hardware import Hardware

    hass = configured_hass
    _, _, _, devices = await agent_bench(hass, monkeypatch)
    hardware = Hardware(arguments(tmp_path))
    hardware.device_id = devices[0].id

    async def command(kind, **fields):
        assert kind == "conversation/process"
        result = await conversation.async_converse(
            hass,
            fields["text"],
            None,
            Context(),
            language="en",
            agent_id=conversation.HOME_ASSISTANT_AGENT,
            device_id=hardware.device_id,
        )
        return result.as_dict()

    for minutes in (5, 10):
        result = await command(
            "conversation/process", text=f"start a timer for {minutes} minutes"
        )
        assert result["response"]["response_type"] == "action_done"
    await hardware.cancel_timers(command)
    await hardware.cancel_timers(command)
    assert hardware.timer_cancel_calls == [{"canceled": 2}, {"canceled": 0}], (
        "NATIVE_HA_CANCEL_COUNTS_RETAINED"
    )
