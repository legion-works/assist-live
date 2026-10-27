import pytest


def test_latency_sensor_exists_without_a_control_path():
    from custom_components.assist_live import PLATFORMS

    assert "sensor" in PLATFORMS


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
