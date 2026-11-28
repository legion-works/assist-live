import pytest


def test_latency_sensor_exists_without_a_control_path():
    from custom_components.assist_live import PLATFORMS

    assert "sensor" in PLATFORMS


def test_latency_is_diagnostic(ledger):
    from homeassistant.const import EntityCategory

    from custom_components.assist_live.sensor import TurnLatencySensor

    assert TurnLatencySensor("satellite-a", "device-a", ledger).entity_category is (
        EntityCategory.DIAGNOSTIC
    )


def test_turn_by_turn_latency_is_enabled_by_default(ledger):
    from custom_components.assist_live.sensor import TurnLatencySensor

    assert TurnLatencySensor(
        "satellite-a", "device-a", ledger
    ).entity_registry_enabled_default


@pytest.mark.parametrize(
    "quality,want",
    [("measured", "measured"), ("proxy", "proxy"), ("estimate", "proxy")],
)
def test_latency_has_separate_quality_and_uncertainty(ledger, quality, want):
    from custom_components.assist_live.sensor import TurnLatencySensor

    turn = ledger.open_voice("satellite-a")
    ledger.observe(
        {
            "turn_id": turn.turn_id,
            "satellite": "satellite-a",
            "stage": "end_decision",
            "t_utc_ns": 1_000_000_000,
            "quality": "measured",
            "uncertainty_ms": 2,
            "data": {},
        }
    )
    ledger.observe(
        {
            "turn_id": turn.turn_id,
            "satellite": "satellite-a",
            "stage": "playback_start",
            "t_utc_ns": 1_125_000_000,
            "quality": quality,
            "uncertainty_ms": 4,
            "data": {},
        }
    )
    sensor = TurnLatencySensor("satellite-a", "device-a", ledger)
    assert sensor.native_value == 125
    assert sensor.native_unit_of_measurement == "ms"
    assert sensor.extra_state_attributes["quality"] == want
    assert sensor.extra_state_attributes["uncertainty_ms"] == 6
    ledger.open_voice("satellite-a")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["quality"] == "missing"


async def test_turn_identity_is_public_before_conversation_and_not_recorded(
    hass, ledger
):
    from homeassistant.helpers.entity_platform import PlatformData

    from custom_components.assist_live.sensor import TurnLatencySensor

    sensor = TurnLatencySensor("satellite-a", "device-a", ledger)
    sensor.hass = hass
    # The test adds the sensor without an entity platform.
    sensor.platform_data = PlatformData(
        hass, domain="sensor", platform_name="assist_live"
    )
    sensor.entity_id = "sensor.test_turn_latency"
    await sensor.async_added_to_hass()
    assert sensor.extra_state_attributes["turn_id"] is None
    first = ledger.open_voice("satellite-a")
    assert hass.states.get(sensor.entity_id).attributes["turn_id"] == first.turn_id
    assert hass.states.get(sensor.entity_id).attributes["turn_type"] == "voice"
    assert sensor.native_value is None
    second = ledger.open_voice("satellite-a")
    assert hass.states.get(sensor.entity_id).attributes["turn_id"] == second.turn_id
    assert {"turn_id", "turn_type"} <= sensor._unrecorded_attributes


async def test_fallback_end_decision_keeps_ha_audio_source(client, ledger):
    from test_stt_forward import Fallback, audio, entity, metadata

    from custom_components.assist_live.sensor import TurnLatencySensor

    await entity(client, ledger, Fallback()).async_process_audio_stream(
        metadata(), audio([bytes(320)])
    )
    record = ledger.latest["satellite-a"]
    start = record.timings["end_decision"]["t_utc_ns"]
    ledger.observe(
        {
            "turn_id": record.turn.turn_id,
            "satellite": "satellite-a",
            "stage": "playback_start",
            "t_utc_ns": start + 125_000_000,
            "quality": "measured",
            "uncertainty_ms": 0,
            "source": "device",
            "data": {},
        }
    )
    sensor = TurnLatencySensor("satellite-a", "device-a", ledger)
    assert sensor.extra_state_attributes["end_decision_source"] == "ha_audio_end"
    assert sensor.extra_state_attributes["quality"] == "proxy"
    ledger.observe(
        {
            "turn_id": record.turn.turn_id,
            "satellite": "satellite-a",
            "stage": "end_decision",
            "t_utc_ns": start - 1_000_000,
            "quality": "measured",
            "uncertainty_ms": 0,
            "source": "server",
            "data": {},
        }
    )
    assert sensor.extra_state_attributes["end_decision_source"] == "ha_audio_end"
    assert sensor.native_value == 125


def test_entities_have_device_context_names(client, ledger):
    from custom_components.assist_live.sensor import TurnLatencySensor
    from custom_components.assist_live.stt import AssistLiveSTT
    from custom_components.assist_live.tts import AssistLiveTTS

    entities = {
        "last_turn_latency": TurnLatencySensor("satellite-a", "device-a", ledger),
        "speech_to_text": AssistLiveSTT(
            "satellite-a", "device-a", client, ledger, "stt.standard", "entry"
        ),
        "text_to_speech": AssistLiveTTS(
            "satellite-a", "device-a", client, ledger, "tts.standard", "entry"
        ),
    }
    for key, entity in entities.items():
        assert entity.has_entity_name, key
        assert entity.translation_key == key
        # An unregistered satellite device keeps a different name for each key.
        assert entity.device_info == {
            "identifiers": {("assist_live", "satellite-a")},
            "name": "satellite-a",
        }, key
