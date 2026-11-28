import base64
import hashlib
import ipaddress
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from test_pipeline_contract import pipeline_hass  # noqa: F401

UNLOADED_ENTRY_DATA = {
    "url": "https://server.example",
    "token": "planted-sensitive-token",
    "fallback_stt": "stt.contract_stt",
    "fallback_tts": "tts.contract_tts",
    "satellites": {"device-a": "satellite-a"},
    "pipeline_selects": {},
    "record_transcripts": True,
}
UNLOADED_ENTRY_OPTIONS = {
    "fallback_tts_voice": "planted-private-voice",
    "local_decisions": {
        "stt.planted_private_entity": {
            "destination": "local",
            "recorded_at": "2026-01-01T00:00:00+00:00",
        }
    },
    "local_only": False,
}


def unloaded_entry():
    return MockConfigEntry(
        domain="assist_live",
        data=UNLOADED_ENTRY_DATA,
        options=UNLOADED_ENTRY_OPTIONS,
    )


def assert_entry_state_only(result):
    assert result["loaded"] is False
    assert result["entry"]["data"]["record_transcripts"] is True
    for key in ("url", "token", "fallback_stt", "fallback_tts", "satellites"):
        assert result["entry"]["data"][key] == "**REDACTED**", key
    assert result["entry"]["options"] == {
        "fallback_tts_voice": "**REDACTED**",
        "local_decisions": "**REDACTED**",
        "local_only": False,
    }
    assert "turns" not in result and "failures" not in result
    serialized = json.dumps(result)
    for value in (
        "server.example",
        "planted-sensitive-token",
        "device-a",
        "planted-private-voice",
        "planted_private_entity",
    ):
        assert value not in serialized, value


# HA 2026.10.4 http/const.py:76 stores a request value with a string key.
# Then aiohttp gives a warning. The pytest filter makes that warning an
# HTTP 500. This mark applies only to the tests that send an HTTP request.
HTTP_REQUEST_KEY_WARNING = pytest.mark.filterwarnings(
    "ignore:It is recommended to use web.RequestKey"
    ":aiohttp.web_exceptions.NotAppKeyWarning"
)


async def hanging_events(*args, **kwargs):
    import asyncio

    await asyncio.Event().wait()
    yield


@HTTP_REQUEST_KEY_WARNING
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_diagnostics_after_unload_give_entry_state_only(
    pipeline_hass,  # noqa: F811
    hass_client,
    client,
):
    hass = pipeline_hass
    client.events = hanging_events
    entry = unloaded_entry()
    entry.add_to_hass(hass)
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        loaded = await get_diagnostics_for_config_entry(hass, hass_client, entry)
        assert "loaded" not in loaded and "failures" in loaded
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert_entry_state_only(
        await get_diagnostics_for_config_entry(hass, hass_client, entry)
    )


@HTTP_REQUEST_KEY_WARNING
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_diagnostics_after_setup_error_give_entry_state_only(
    pipeline_hass,  # noqa: F811
    hass_client,
    client,
):
    from custom_components.assist_live import Runtime

    hass = pipeline_hass
    client.events = hanging_events
    entry = unloaded_entry()
    entry.add_to_hass(hass)
    with (
        patch("custom_components.assist_live.AssistLiveClient", return_value=client),
        patch.object(Runtime, "_maintenance", side_effect=RuntimeError("test")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert_entry_state_only(
        await get_diagnostics_for_config_entry(hass, hass_client, entry)
    )


async def test_diagnostics_do_not_expose_redacted_satellite_identities(hass, client):
    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    satellites = {"test-device-one": "0-test-name", "test-device-two": "1-test-name"}
    entry = MockConfigEntry(domain="assist_live", data={"satellites": satellites})
    runtime = entry.runtime_data = Runtime(hass, entry, client)
    turns = {
        satellite: runtime.ledger.open_voice(satellite).turn_id
        for satellite in satellites.values()
    }
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    dump = json.dumps(diagnostics)
    identities = [
        identity
        for device_id, name in satellites.items()
        for identity in (name, device_id, f"{turns[name]}:{name}")
    ]
    for identity in identities:
        raw = identity.encode()
        assert hashlib.sha256(raw).hexdigest() not in dump
        assert raw.hex() not in dump
        assert base64.b64encode(raw).decode() not in dump
    assert all(identity not in dump for identity in identities)
    assert {turn["turn_id"] for turn in diagnostics["turns"]} == set(turns.values())
    assert all(turn["satellite"] == "[redacted]" for turn in diagnostics["turns"])
    assert len(diagnostics["stt_hints"]) == len(satellites)


@pytest.mark.parametrize("selection", ["explicit", "manual", "preferred"])
async def test_diagnostics_report_the_selected_agent(
    pipeline_hass,  # noqa: F811
    client,
    selection,
):
    from homeassistant.components import assist_pipeline

    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    hass = pipeline_hass
    preferred = assist_pipeline.async_get_pipeline(hass)
    other = await assist_pipeline.async_create_default_pipeline(
        hass, "stt.contract_stt", "tts.contract_tts", "Other pipeline"
    )
    assert other is not None
    cloud = preferred if selection == "preferred" else other
    await assist_pipeline.async_update_pipeline(
        hass, cloud, conversation_engine="conversation.synthetic_cloud"
    )
    data = {
        "satellites": {"device-a": "satellite-a"},
        "pipeline_selects": {"device-a": "select.pipeline_a"},
    }
    if selection != "manual":
        saved = other if selection == "preferred" else preferred
        data["pipelines"] = {"device-a": saved.id}
    option = (
        assist_pipeline.OPTION_PREFERRED if selection == "preferred" else other.name
    )
    hass.states.async_set("select.pipeline_a", option)
    entry = MockConfigEntry(domain="assist_live", data=data)
    entry.runtime_data = Runtime(hass, entry, client)

    async def iot_class(hass, entity_id):
        return "cloud_push" if entity_id == "conversation.synthetic_cloud" else None

    with patch(
        "custom_components.assist_live.diagnostics.entity_iot_class",
        side_effect=iot_class,
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["agents"] == [{"iot_class": "cloud_push", "destination": "cloud"}]
    assert "conversation.synthetic_cloud" not in json.dumps(result)


async def test_diagnostics_do_not_change_fallback_metadata_or_repair(
    pipeline_hass,  # noqa: F811
    client,
):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )
    from custom_components.assist_live.repairs import fallback_issue
    from custom_components.assist_live.tts import AssistLiveTTS

    hass = pipeline_hass
    fallback = "tts.contract_tts"
    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = entry.runtime_data = Runtime(hass, entry, client)
    metadata = {"entity_id": fallback, "languages": []}
    runtime.fallback_metadata["tts"] = metadata
    forwarder = AssistLiveTTS(
        "satellite-a",
        "device-a",
        client,
        runtime.ledger,
        fallback,
        entry.entry_id,
        metadata,
    )
    forwarder.hass = hass
    runtime.tts["device-a"] = forwarder
    fallback_issue(hass, entry.entry_id, "tts", fallback, True)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert ir.async_get(hass).async_get_issue(
        "assist_live", f"{entry.entry_id}_fallback_tts_unavailable"
    )
    assert metadata == {"entity_id": fallback, "languages": []}
    assert sum(result["tts_fallback"].values()) == 1


async def test_diagnostics_sanitize_decisions_and_exclude_unavailable_engines(
    pipeline_hass,  # noqa: F811
    client,
):
    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )
    from custom_components.assist_live.tts import AssistLiveTTS, fallback_engine

    hass = pipeline_hass
    stt_id, tts_id = "stt.contract_stt", "tts.contract_tts"
    entry = MockConfigEntry(
        domain="assist_live",
        data={"satellites": {}, "fallback_stt": stt_id, "fallback_tts": tts_id},
        options={
            "local_decisions": {
                stt_id: {"destination": "local", "recorded_at": "not a date"}
            }
        },
    )
    runtime = entry.runtime_data = Runtime(hass, entry, client)
    runtime.tts["device-a"] = AssistLiveTTS(
        "satellite-a", "device-a", client, runtime.ledger, tts_id, entry.entry_id
    )
    fallback_engine(hass, tts_id)._attr_available = False
    with patch(
        "custom_components.assist_live.diagnostics.entity_iot_class",
        side_effect=["unrecognized.label", None],
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["fallbacks"]["stt"] == {
        "destination": "local",
        "iot_class": "[redacted]",
    }
    assert result["local_decisions"] == [
        {"part": "stt", "destination": "local", "recorded_at": None}
    ]
    assert sum(result["tts_fallback"].values()) == 0


@pytest.mark.parametrize("failure", [TimeoutError(), OSError(), "unhealthy"])
async def test_diagnostics_health_outage_and_recovery(hass, client, failure):
    from assist_live_client.models import HealthCapabilities, TTSVoice

    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    entry.runtime_data = runtime
    now = [100.0]
    runtime.clock = lambda: now[0]
    first = datetime(2026, 1, 1, tzinfo=UTC)
    recovered = datetime(2026, 1, 1, 0, 3, tzinfo=UTC)
    client.health_capabilities.side_effect = [
        HealthCapabilities(
            True,
            local_only=True,
            destinations={"tts": "local"},
            tts_voices=(TTSVoice("server-us", "US", "en-US"),),
            tts_default_voice="server-us",
        ),
        HealthCapabilities(False, local_only=False)
        if failure == "unhealthy"
        else failure,
        HealthCapabilities(True, local_only=False, destinations={"tts": "cloud"}),
    ]
    with patch("homeassistant.util.dt.utcnow", side_effect=[first, recovered]):
        await runtime.refresh_health()
        healthy = await async_get_config_entry_diagnostics(hass, entry)
        assert healthy["server_healthy"] is True, "current_health_success"
        assert healthy["server_last_healthy"] == first.isoformat(), "success_timestamp"
        expected = {
            "local_only": True,
            "destinations": {"tts": "local"},
            "tts_voice_count": 1,
            "tts_default_voice": "server-us",
        }
        assert healthy["server_last_known"] == expected
        now[0] = 130.0
        await runtime.refresh_health()
        failed = await async_get_config_entry_diagnostics(hass, entry)
        assert failed["server_healthy"] is False, "current_health_not_cached_health"
        assert failed["server_last_healthy"] == first.isoformat(), (
            "outage_keeps_timestamp"
        )
        assert failed["server_last_known"] == expected, "outage_keeps_last_known"
        now[0] = 280.0
        await runtime.refresh_health()
        result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["server_healthy"] is True, "recovery_health"
    assert result["server_last_healthy"] == recovered.isoformat(), (
        "recovery_updates_time"
    )
    assert result["server_last_known"]["local_only"] is False
    assert result["server_last_known"]["destinations"] == {"tts": "cloud"}
    assert (
        not {
            "server_local_only",
            "server_destinations",
            "server_tts_voice_count",
            "server_tts_default_voice",
        }
        & result.keys()
    ), "no_unmarked_response_values"


@pytest.mark.parametrize("failure", [TimeoutError(), "unhealthy"])
async def test_diagnostics_never_healthy_has_no_last_known_values(
    hass, client, failure
):
    from assist_live_client.models import HealthCapabilities

    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    entry.runtime_data = runtime
    for checked in (False, True):
        if checked:
            client.health_capabilities.side_effect = [
                HealthCapabilities(False, local_only=True)
                if failure == "unhealthy"
                else failure
            ]
            await runtime.refresh_health()
        result = await async_get_config_entry_diagnostics(hass, entry)
        assert result["server_healthy"] is False, "never_healthy"
        assert result["server_last_healthy"] is None, "never_healthy_timestamp"
        assert result["server_last_known"] is None, "never_healthy_values_not_reported"


async def test_diagnostics_exclude_all_content_and_private_configuration(hass, client):
    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    planted = {
        "token": "synthetic-token-content",
        "audio": "synthetic-audio-content",
        "transcript": "synthetic-transcript-content",
        "text": "synthetic-tts-content",
        "name": "synthetic-speaker-content",
        "url": "http://server.invalid",
        "private_address": str(ipaddress.ip_address(0x0A000007)),
        "fallback_stt": "stt.planted-fallback",
        "fallback_tts": "tts.planted-fallback",
        "conversation_entity": "conversation.planted",
    }
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            **planted,
            "satellites": {"device-a": "satellite-a"},
            "pipeline_selects": {"device-a": "select.pipeline_a"},
        },
    )
    runtime = Runtime(hass, entry, client)
    turn = runtime.ledger.open_voice("satellite-a")
    runtime.ledger.records[turn.turn_id].speaker = planted
    runtime.ledger.used(turn.turn_id, "stt_fallback", planted["fallback_stt"], "local")
    runtime.ledger.used(turn.turn_id, "tts_fallback", planted["fallback_tts"], "local")
    runtime.ledger.records[turn.turn_id].audio_format = {
        **planted,
        "codec": "pcm_s16le",
        "rate": 22050,
        "channels": 1,
        "width": 2,
    }
    entry.runtime_data = runtime
    hass.states.async_set("select.pipeline_a", "Pipeline A")
    with (
        patch(
            "custom_components.assist_live.diagnostics.assist_pipeline.async_get_pipelines",
            return_value=[
                SimpleNamespace(
                    id="pipeline-a",
                    name="Pipeline A",
                    conversation_engine=planted["conversation_entity"],
                )
            ],
        ) as pipelines,
        patch(
            "custom_components.assist_live.diagnostics.entity_iot_class",
            return_value="local_push",
        ) as iot,
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)
    pipelines.assert_called_once_with(hass)
    assert {call.args[1] for call in iot.call_args_list} == {
        planted["fallback_stt"],
        planted["fallback_tts"],
        planted["conversation_entity"],
    }
    serialized = json.dumps(result)
    for value in planted.values():
        assert value not in serialized
    assert result["agent_boundary"] == "outside_local_only"
    assert [row["provider"] for row in result["turns"][0]["destinations"]] == [
        "ha_entity",
        "ha_entity",
    ]
    assert result["turns"][0]["satellite"] == "satellite-a"


async def test_diagnostics_show_actual_turn_services_and_agent_boundary(hass, client):
    from types import SimpleNamespace
    from unittest.mock import patch

    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "satellites": {"device-a": "satellite-a"},
            "fallback_stt": "stt.standard",
            "fallback_tts": "tts.standard",
            "pipeline_selects": {"device-a": "select.pipeline_a"},
        },
        options={
            "local_only": True,
            "local_decisions": {
                "stt.standard": {
                    "destination": "local",
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                }
            },
        },
    )
    runtime = Runtime(hass, entry, client)
    turn = runtime.ledger.open_voice("satellite-a")
    runtime.ledger.used(turn.turn_id, "stt", "server", "unknown")
    runtime.ledger.observe(
        {
            "turn_id": turn.turn_id,
            "satellite": "satellite-a",
            "stage": "stt_final",
            "data": {"part": "stt", "provider": "parakeet", "destination": "local"},
        }
    )
    runtime.ledger.used(turn.turn_id, "stt_fallback", "stt.standard", "local")
    runtime.ledger.observe(
        {
            "turn_id": turn.turn_id,
            "satellite": "satellite-a",
            "stage": "tts_first_byte",
            "data": {"part": "tts", "provider": "piper", "destination": "local"},
        }
    )
    record = runtime.ledger.latest["satellite-a"]
    record.voice = "en_US-lessac-medium"
    record.audio_format = {
        "codec": "pcm_s16le",
        "rate": 22050,
        "channels": 1,
        "width": 2,
    }
    entry.runtime_data = runtime
    hass.states.async_set("select.pipeline_a", "Pipeline A")

    async def iot(hass, entity_id):
        return "cloud_push" if entity_id == "conversation.agent" else None

    with (
        patch(
            "custom_components.assist_live.diagnostics.entity_iot_class",
            side_effect=iot,
        ),
        patch(
            "custom_components.assist_live.diagnostics.assist_pipeline.async_get_pipelines",
            return_value=[
                SimpleNamespace(
                    id="pipeline-a",
                    name="Pipeline A",
                    conversation_engine="conversation.agent",
                )
            ],
        ),
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["agents"] == [{"iot_class": "cloud_push", "destination": "cloud"}]
    assert result["agent_boundary"] == "outside_local_only"
    assert result["local_decisions"][0]["recorded_at"] == "2026-01-01T00:00:00+00:00"
    assert result["turns"][0]["destinations"] == [
        {"part": "stt", "provider": "parakeet", "destination": "local"},
        {"part": "stt_fallback", "provider": "ha_entity", "destination": "local"},
        {"part": "tts", "provider": "piper", "destination": "local"},
    ]
    assert result["turns"][0]["voice"] == "en_US-lessac-medium"
    assert result["turns"][0]["audio_format"]["rate"] == 22050
    assert result["turns"][0]["tts_text_input"] == "sentence_buffered"


async def test_health_destination_keys_do_not_disclose_content(
    hass, socket_enabled, relaxed_health_timeout
):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from assist_live_client import AssistLiveClient

    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    planted = [
        "synthetic-token-content",
        "synthetic-transcript-content",
        "https://server.invalid",
        "stt.synthetic-entity",
    ]
    permitted = {"stt": "local", "tts": "cloud", "speaker": "unknown"}

    async def health(request):
        return web.json_response(
            {
                "version": 1,
                "healthy": True,
                "local_only": True,
                "destinations": {**permitted, **dict.fromkeys(planted, "local")},
            }
        )

    app = web.Application()
    app.router.add_get("/health", health)
    async with (
        TestServer(app) as server,
        AssistLiveClient(str(server.make_url("")), "test-only") as client,
    ):
        entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
        runtime = Runtime(hass, entry, client)
        await runtime.refresh_health()
        entry.runtime_data = runtime
        result = await async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(result)
    for value in planted:
        assert value not in serialized
    assert result["server_last_known"]["destinations"] == permitted, (
        "destination_key_allowlist"
    )


@pytest.mark.parametrize("part", ["stt", "tts", "speaker"])
async def test_diagnostics_drop_unrecognized_health_destination_values(
    hass, client, part
):
    from assist_live_client.models import HealthCapabilities

    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    client.health_capabilities.return_value = HealthCapabilities(
        True,
        destinations={
            part: "synthetic-unrecognized-destination",
            "synthetic-token-content": "local",
        },
    )
    await runtime.refresh_health()
    entry.runtime_data = runtime
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["server_last_known"]["destinations"] == {}, (
        "destination_value_allowlist"
    )


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("not_local_only", "not_local_only"),
        ("destination_not_local", "destination_not_local"),
        ("health_error", "health_error"),
        ("stream_error", "stream_error"),
        ("server_not_local_only", "unknown"),
        ("server_destination_not_local", "unknown"),
        ("synthetic-secret-content", "unknown"),
    ],
)
async def test_fallback_reason_is_visible_per_turn_and_allowlisted(
    hass, client, reason, expected
):
    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    turn = runtime.ledger.open_voice("satellite-a")
    runtime.ledger.records[turn.turn_id].fallback_reasons["stt"] = reason
    runtime.ledger.used(turn.turn_id, "stt_fallback", "stt.standard", "local")
    runtime.ledger.mark(turn.turn_id, "fallback_used", reason="server_unhealthy")
    entry.runtime_data = runtime
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["turns"][0]["destinations"][0]["fallback_reason"] == expected
    assert "synthetic-secret-content" not in json.dumps(result)


async def test_diagnostic_turn_ids_match_latency_sensor_correlation_ids(hass, client):
    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )
    from custom_components.assist_live.sensor import TurnLatencySensor

    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    first = runtime.ledger.open_voice("satellite-a")
    second = runtime.ledger.open_voice("satellite-b")
    entry.runtime_data = runtime
    result = await async_get_config_entry_diagnostics(hass, entry)
    by_satellite = {row["satellite"]: row for row in result["turns"]}
    assert first.turn_id != second.turn_id
    for turn in (first, second):
        sensor = TurnLatencySensor(turn.satellite, "device", runtime.ledger)
        assert (
            by_satellite[turn.satellite].get("turn_id")
            == (sensor.extra_state_attributes["turn_id"])
            == turn.turn_id
        )
