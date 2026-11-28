from contextlib import aclosing
from unittest.mock import AsyncMock, Mock

import pytest
from assist_live_client.protocol import ProtocolError
from homeassistant.helpers import device_registry as dr
from test_pipeline_contract import pipeline_hass  # noqa: F401
from test_runtime_reload import runtime_entry  # noqa: F401

from custom_components.assist_live.server_auth import ServerAuth


@pytest.fixture
def disable_periodic_maintenance(monkeypatch):
    tracker = Mock(return_value=Mock())
    monkeypatch.setattr(
        "custom_components.assist_live.async_track_time_interval", tracker
    )
    yield
    tracker.assert_called_once()


async def test_new_timing_after_refused_open_schedules_delivery(
    ledger, client, drain_timing_events
):
    client.open_turn.side_effect = ProtocolError("open refused", kind="refused")
    turn = ledger.open_voice("satellite-a")
    ledger.schedule_events(retry=True)
    await drain_timing_events(ledger)
    client.open_turn.assert_awaited_once_with(turn)
    client.event.assert_not_awaited()

    ledger.mark(turn.turn_id, "stt_final_received")
    ledger.schedule_events()
    if sender := ledger._event_sender:
        await sender

    client.event.assert_awaited_once()
    event = client.event.await_args.args[0]
    assert (event.turn_id, event.stage) == (turn.turn_id, "stt_final_received")
    client.open_turn.assert_awaited_once_with(turn)
    client.health_capabilities.assert_not_awaited()


@pytest.mark.usefixtures("disable_periodic_maintenance")
async def test_satellite_state_sends_without_maintenance(
    runtime_entry,  # noqa: F811
    client,
    drain_timing_events,
):
    hass, entry, _, _ = runtime_entry
    ledger = entry.runtime_data.ledger
    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    await drain_timing_events(ledger)
    client.event.reset_mock()

    hass.states.async_set("assist_satellite.synthetic_voice", "responding")
    await hass.async_block_till_done()
    if sender := ledger._event_sender:
        await sender

    client.event.assert_awaited_once()
    event = client.event.await_args.args[0]
    assert (event.turn_id, event.satellite, event.source, event.stage) == (
        turn.turn_id,
        "satellite-a",
        "ha_state",
        "satellite_state",
    )
    assert event.data == {"state": "responding"}


async def test_speaker_dedup_holds_last_128_turns(ledger):
    delivered = []
    ledger.speaker_event = delivered.append
    result = {"name": "speaker", "distance": 0.1, "destination": "local"}
    turn_ids = [f"turn-{index}" for index in range(129)]

    for turn_id in turn_ids[:128]:
        ledger.speaker_result(turn_id, result, satellite="satellite-a")
    ledger.speaker_result(turn_ids[0], result, satellite="satellite-a")
    assert [event["turn_id"] for event in delivered] == turn_ids[:128]

    ledger.speaker_result(turn_ids[128], result, satellite="satellite-a")
    ledger.speaker_result(turn_ids[0], result, satellite="satellite-a")
    assert [event["turn_id"] for event in delivered] == [*turn_ids, turn_ids[0]]


async def test_setup_keeps_empty_parent_of_child_device(runtime_entry):  # noqa: F811
    hass, entry, _, _ = runtime_entry
    devices = dr.async_get(hass)
    parent = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("assist_live", "parent")}
    )
    child = devices.async_get_or_create_child(
        config_entry_id=entry.entry_id,
        identifiers={("assist_live", "child")},
        parent_device_id=parent.id,
    )
    leaf = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("assist_live", "leaf")}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)

    assert devices.async_get(leaf.id) is None
    assert devices.async_get(parent.id) is not None
    assert dr.async_entries_for_parent_device(devices, parent.id) == [child]


async def test_rejected_token_cannot_start_new_streams():
    auth = ServerAuth()
    request = AsyncMock(side_effect=ProtocolError("token refused", kind="unauthorized"))
    with pytest.raises(ProtocolError) as refusal:
        await auth.request(request)
    assert refusal.value.kind == "unauthorized"

    async def server_stream():
        yield "response"

    factory = Mock(side_effect=server_stream)
    async with aclosing(auth.stream(factory)) as stream:
        with pytest.raises(ProtocolError) as rejected:
            await anext(stream)
    assert rejected.value.kind == "unauthorized"
    factory.assert_not_called()
