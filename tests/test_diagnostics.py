import ipaddress
import json
from types import SimpleNamespace
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry


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
