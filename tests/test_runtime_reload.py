import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_pipeline_contract import pipeline_hass  # noqa: F401

VOICE_MAC = (dr.CONNECTION_NETWORK_MAC, "02:00:00:00:00:01")


@pytest.fixture
async def runtime_entry(pipeline_hass, client, enable_custom_integrations):  # noqa: F811
    hass = pipeline_hass

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events
    voice_entry = MockConfigEntry(domain="esphome")
    voice_entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    linked = devices.async_get_or_create(
        config_entry_id=voice_entry.entry_id,
        connections={VOICE_MAC},
        name="Voice satellite",
        manufacturer="Test manufacturer",
    )
    entry = MockConfigEntry(
        domain="assist_live",
        title="assist-live",
        data={
            "url": "https://server.example",
            "token": "test-only",
            "fallback_stt": "stt.contract_stt",
            "fallback_tts": "tts.contract_tts",
            "satellites": {linked.id: "satellite-a", "absent-device": "satellite-b"},
            "pipeline_selects": {},
        },
    )
    entry.add_to_hass(hass)
    old_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, connections={VOICE_MAC}
    )
    old_fallback = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("assist_live", "absent-device")},
        name="satellite-b",
    )
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "assist_satellite",
        "esphome",
        "synthetic-voice",
        config_entry=voice_entry,
        device_id=linked.id,
        suggested_object_id="synthetic_voice",
    )
    old_ids = {
        f"satellite-{key}_{suffix}": registry.async_get_or_create(
            domain,
            "assist_live",
            f"satellite-{key}_{suffix}",
            config_entry=entry,
            device_id=device.id,
            suggested_object_id=f"assist_live_satellite_{key}{object_suffix}",
        ).entity_id
        for key, device in (("a", old_device), ("b", old_fallback))
        for domain, suffix, object_suffix in (
            ("stt", "stt", ""),
            ("tts", "tts", ""),
            ("sensor", "last_turn_latency", "_last_turn_latency"),
        )
    }
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            yield hass, entry, linked, old_ids
        finally:
            if entry.state is ConfigEntryState.LOADED:
                assert await hass.config_entries.async_unload(entry.entry_id)


async def test_latency_sensor_reports_server_outage_but_speech_fallbacks_stay_available(
    runtime_entry, client
):
    from assist_live_client.models import HealthCapabilities

    hass, entry, _, old_ids = runtime_entry
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    now = 0.0
    runtime.clock = lambda: now
    runtime._health_due = 5.0
    latency = old_ids["satellite-a_last_turn_latency"]

    def assert_states(expected_latency):
        assert hass.states.get(latency).state == expected_latency
        for suffix in ("stt", "tts"):
            assert (
                hass.states.get(old_ids[f"satellite-a_{suffix}"]).state
                != STATE_UNAVAILABLE
            )

    assert_states(STATE_UNAVAILABLE)
    now += 5
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    await runtime.refresh_health()
    assert_states(STATE_UNKNOWN)
    now += 30
    client.health_capabilities.side_effect = OSError
    await runtime.refresh_health()
    assert_states(STATE_UNAVAILABLE)


async def test_final_turn_store_fault_keeps_options_reload_working(
    runtime_entry, caplog
):
    hass, entry, linked, old_ids = runtime_entry
    prior = entry.runtime_data
    await hass.async_block_till_done()
    prior.cursors["satellite-a"] = 42
    write = Store._async_write_data
    attempts = []

    async def fail_turn_saves(store, data):
        if store.key == prior.store.key:
            attempts.append(data)
            raise WriteError("synthetic turn store fault")
        await write(store, data)

    with patch.object(Store, "_async_write_data", fail_turn_saves):
        hass.config_entries.async_update_entry(
            entry, options={"fallback_stt": "stt.alternate_stt"}
        )
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        runtime = entry.runtime_data
        assert runtime is not prior
        assert runtime.stt[linked.id].fallback_entity_id == "stt.alternate_stt"
        assert runtime.stt[linked.id].entity_id == old_ids["satellite-a_stt"]
        assert runtime.cursors == {"satellite-a": 42}
        assert len(attempts) >= 2
        assert "Final turn store save failed" in caplog.text

    runtime._store_retry_due = 0
    runtime.cursors["satellite-a"] = 50
    await runtime._maintenance(None)
    saved = await runtime.store.async_load()
    assert saved["cursors"] == {"satellite-a": 50}
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.runtime_data is not runtime
    assert entry.runtime_data.cursors == {"satellite-a": 50}


async def test_reconfigure_reloads_a_loaded_entry_without_replacing_entities(
    runtime_entry, client, caplog
):
    hass, entry, linked, old_ids = runtime_entry
    old_runtime = entry.runtime_data
    url = "https://new.example"
    validator = AsyncMock()
    validator.bindings.return_value = [
        {"satellite": "satellite-a"},
        {"satellite": "satellite-b"},
    ]
    with (
        patch(
            "custom_components.assist_live.config_flow.AssistLiveClient",
            return_value=validator,
        ),
        patch(
            "custom_components.assist_live.AssistLiveClient", return_value=client
        ) as create_runtime,
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload,
    ):
        flows = hass.config_entries.flow
        form = await flows.async_init(
            "assist_live",
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        result = await flows.async_configure(form["flow_id"], {"url": url})
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
    create_runtime.assert_called_once_with(
        url, "test-only", session=async_get_clientsession(hass)
    )
    reload.assert_awaited_once_with(entry.entry_id)
    assert "should use it for scheduling a reload" not in caplog.text
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not old_runtime
    assert entry.runtime_data.stt[linked.id].entity_id == old_ids["satellite-a_stt"]
    assert entry.runtime_data.tts[linked.id].entity_id == old_ids["satellite-a_tts"]
    assert all(hass.states.get(entity_id) is not None for entity_id in old_ids.values())


async def test_satellite_state_updates_only_its_own_turn(runtime_entry):
    hass, entry, _, _ = runtime_entry
    runtime = entry.runtime_data
    a = runtime.ledger.open_voice("satellite-a")
    b = runtime.ledger.open_voice("satellite-b")
    hass.states.async_set("assist_satellite.synthetic_voice", "responding")
    await hass.async_block_till_done()
    assert "satellite_state" in runtime.ledger.records[a.turn_id].stages
    assert "satellite_state" not in runtime.ledger.records[b.turn_id].stages
    runtime.ledger.open_voice("satellite-a")
    hass.states.async_remove("assist_satellite.synthetic_voice")
    await hass.async_block_till_done()
    assert "satellite_state" not in runtime.ledger.latest["satellite-a"].stages


@pytest.mark.parametrize("state", [STATE_UNAVAILABLE, STATE_UNKNOWN])
async def test_satellite_listener_ignores_non_protocol_states(
    runtime_entry, caplog, state
):
    hass, entry, _, _ = runtime_entry
    ledger = entry.runtime_data.ledger
    turn = ledger.open_voice("satellite-a")

    hass.states.async_set("assist_satellite.synthetic_voice", state)
    await hass.async_block_till_done()

    assert not [record.exc_info for record in caplog.records if record.exc_info]
    assert "satellite_state" not in ledger.records[turn.turn_id].stages
    assert not any(event.stage == "satellite_state" for event in ledger._pending)


async def test_setup_removes_empty_legacy_devices(runtime_entry):
    hass, entry, linked, old_ids = runtime_entry
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    assert devices.async_get_device_by_connection(VOICE_MAC, entry.entry_id) is None
    assert (
        devices.async_get_device_by_identifier(
            ("assist_live", "absent-device"), entry.entry_id
        )
        is None
    )
    assert devices.async_get(linked.id) == linked
    voice = registry.async_get("assist_satellite.synthetic_voice")
    assert voice.device_id == linked.id
    assert voice.config_entry_id == linked.config_entry_id != entry.entry_id
    assert {
        row.unique_id: row.entity_id
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
    } == old_ids


async def test_setup_removes_stale_device_and_its_owned_entities(runtime_entry):
    hass, entry, linked, old_ids = runtime_entry
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    stale = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("assist_live", "retired")}
    )
    orphan = registry.async_get_or_create(
        "sensor",
        "assist_live",
        "retired_last_turn_latency",
        config_entry=entry,
        device_id=stale.id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert devices.async_get(stale.id) is None
    assert registry.async_get(orphan.entity_id) is None
    assert devices.async_get(linked.id) == linked
    assert all(
        registry.async_get(entity_id) is not None for entity_id in old_ids.values()
    )


async def test_setup_removes_empty_fallback_without_removing_linked_device(
    runtime_entry,
):
    hass, entry, linked, _ = runtime_entry
    devices = dr.async_get(hass)
    fallback = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("assist_live", "satellite-a")},
        connections={VOICE_MAC},
    )
    assert fallback.id != linked.id
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert devices.async_get(fallback.id) is None
    assert devices.async_get(linked.id) == linked


async def test_manual_removal_refuses_legacy_composite_view(runtime_entry):
    from attr import evolve

    from custom_components.assist_live import async_remove_config_entry_device

    hass, entry, linked, _ = runtime_entry
    devices = dr.async_get(hass)
    owned = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("assist_live", "retired")}
    )
    composite = evolve(
        owned,
        id="legacy-composite-id",
        composite_subentries={entry.entry_id: {None}, linked.config_entry_id: {None}},
    )
    assert not await async_remove_config_entry_device(hass, entry, composite)
    assert devices.async_get(linked.id) == linked


async def test_user_can_remove_only_devices_with_no_active_or_foreign_entities(
    runtime_entry,
):
    from custom_components.assist_live import async_remove_config_entry_device

    hass, entry, linked, _ = runtime_entry
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    empty = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("assist_live", "unused")}
    )
    assert await async_remove_config_entry_device(hass, entry, empty)
    assert not await async_remove_config_entry_device(hass, entry, linked)
    foreign = registry.async_get_or_create(
        "sensor",
        "esphome",
        "other-entity",
        config_entry=hass.config_entries.async_get_entry(linked.config_entry_id),
        device_id=empty.id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    assert not await async_remove_config_entry_device(hass, entry, empty)
    registry.async_remove(foreign.entity_id)
    orphan = registry.async_get_or_create(
        "stt", "assist_live", "unused_stt", config_entry=entry, device_id=empty.id
    )
    assert await async_remove_config_entry_device(hass, entry, empty)
    assert registry.async_get(orphan.entity_id) is None


async def test_satellite_devices_and_names_keep_existing_registry_ids(runtime_entry):
    hass, entry, linked, old_ids = runtime_entry
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    rows = {
        row.unique_id: row
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert devices.async_get(linked.id) == linked
    assert rows["satellite-a_tts"].device_id == linked.id
    fallback = devices.async_get_device_by_identifier(
        ("assist_live", "satellite-b"), entry.entry_id
    )
    assert fallback is not None and fallback.id != linked.id
    for suffix, name in (
        ("stt", "STT"),
        ("tts", "TTS"),
        ("last_turn_latency", "Last turn latency"),
    ):
        a = rows[f"satellite-a_{suffix}"]
        b = rows[f"satellite-b_{suffix}"]
        assert a.entity_id == old_ids[a.unique_id]
        assert a.device_id == linked.id
        assert b.device_id == fallback.id
        assert a.has_entity_name and b.has_entity_name
        assert a.original_name == b.original_name == name
        assert hass.states.get(a.entity_id).attributes["friendly_name"] == (
            f"Voice satellite {name}"
        )
        assert hass.states.get(b.entity_id).attributes["friendly_name"] == (
            f"satellite-b {name}"
        )
