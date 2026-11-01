import ipaddress
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.mark.parametrize("failure", [TimeoutError(), OSError(), "unhealthy"])
async def test_diagnostics_health_outage_and_recovery(hass, client, failure):
    from assist_live_client.models import HealthCapabilities, TTSVoice

    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    hass.data["assist_live"] = {entry.entry_id: runtime}
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
    hass.data["assist_live"] = {entry.entry_id: runtime}
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
            "pipelines": {"device-a": "pipeline-a"},
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
    hass.data["assist_live"] = {entry.entry_id: runtime}
    with (
        patch(
            "custom_components.assist_live.diagnostics.assist_pipeline.async_get_pipelines",
            return_value=[
                SimpleNamespace(
                    id="pipeline-a", conversation_engine=planted["conversation_entity"]
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
            "pipelines": {"device-a": "pipeline-a"},
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
    hass.data["assist_live"] = {entry.entry_id: runtime}

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
                    id="pipeline-a", conversation_engine="conversation.agent"
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


async def test_health_destination_keys_do_not_disclose_content(hass, socket_enabled):
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
        hass.data["assist_live"] = {entry.entry_id: runtime}
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
    hass.data["assist_live"] = {entry.entry_id: runtime}
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
    hass.data["assist_live"] = {entry.entry_id: runtime}
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["turns"][0]["destinations"][0]["fallback_reason"] == expected
    assert "synthetic-secret-content" not in json.dumps(result)
