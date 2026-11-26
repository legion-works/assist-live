import pytest

from tests.test_fault_hardware import arguments
from tests.test_report import dataset
from tools import fault_hardware
from tools.device_replay import internal_stage_errors, owned_internal_event
from tools.fault_hardware import Hardware, after_final_stop_in_run


def test_reply_stop_can_complete_after_pipeline_end():
    row = {
        "events": [
            {"stage": "stt-end", "t_utc_ns": 10},
            {"stage": "run-end", "t_utc_ns": 15},
        ],
        "final_observed_utc_ns": 20,
        "fault_requested_utc_ns": 21,
        "fault_stopped_utc_ns": 25,
        "server_reply_end_utc_ns": 23,
        "device_playback_end_utc_ns": 30,
    }
    assert after_final_stop_in_run(row), "STOP_DURING_REPLY"
    row["server_reply_end_utc_ns"] = 21
    assert not after_final_stop_in_run(row), "REQUEST_BEFORE_SERVER_REPLY_END"
    row["server_reply_end_utc_ns"] = None
    assert after_final_stop_in_run(row)
    row["device_playback_end_utc_ns"] = 25
    assert not after_final_stop_in_run(row), "STOP_BEFORE_PLAYBACK_END"
    row["device_playback_end_utc_ns"] = None
    assert not after_final_stop_in_run(row)
    row["satellite_reply_idle_utc_ns"] = 29
    assert after_final_stop_in_run(row)


@pytest.mark.parametrize(
    "mode",
    [
        "failed",
        "wrong_turn",
        "malformed",
        "partial",
        "no_reply_end",
        "late",
        "unconfigured",
    ],
)
async def test_after_final_server_read_status(tmp_path, monkeypatch, mode):
    row = {
        "stage": "after-final",
        "turn_id": "test-turn",
        "events": [{"stage": "stt-end", "t_utc_ns": 10}],
        "final_observed_utc_ns": 20,
        "fault_requested_utc_ns": 21,
        "fault_stopped_utc_ns": 25,
        "satellite_reply_idle_utc_ns": 30,
        "correlation": {"complete": True},
    }
    monkeypatch.delenv("ASSIST_LIVE_URL", raising=False)
    monkeypatch.delenv("ASSIST_LIVE_TOKEN_FILE", raising=False)
    if mode != "unconfigured":
        token_file = tmp_path / "auth.json"
        token_file.write_text('{"token":"synthetic-test-token"}')
        monkeypatch.setenv("ASSIST_LIVE_URL", "http://127.0.0.1:1")
        if mode != "partial":
            monkeypatch.setenv("ASSIST_LIVE_TOKEN_FILE", str(token_file))

    class Client:
        def __init__(self, *_):
            assert mode not in ("unconfigured", "partial")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def turn(self, turn_id):
            if mode == "failed":
                raise TimeoutError
            return {
                "turn_id": "other-turn" if mode == "wrong_turn" else turn_id,
                "events": [marker(row, "tts_last_byte", "server", 21)]
                if mode == "late"
                else None
                if mode == "malformed"
                else [],
            }

    monkeypatch.setattr(fault_hardware, "AssistLiveClient", Client)
    await fault_hardware.read_reply_bounds(row)
    if mode in ("failed", "wrong_turn", "malformed", "partial"):
        assert fault_hardware.injection_status(row) == "invalid_injection", (
            "SERVER_READ_FAILURE_IS_INVALID"
        )
        assert row["hardware_invalid_reason"] == "server_read_failed"
        assert row["after_final_bound_source"] == "server_read_failed"
    elif mode == "late":
        assert fault_hardware.injection_status(row) == "invalid_injection"
        assert row["hardware_invalid_reason"] == "after_final_stop_order"
        assert row["after_final_bound_source"] == "server_and_turn_end"
    else:
        assert fault_hardware.injection_status(row) == "verified"
        assert row["after_final_bound_source"] == (
            "turn_end_only" if mode == "unconfigured" else "server_read_no_reply_end"
        )


def marker(row, stage, source, stamp, **data):
    return {
        "turn_id": row["turn_id"],
        "stage": stage,
        "source": source,
        "t_utc_ns": stamp,
        "data": data,
    }


def test_tts_completeness_follows_observed_path():
    _, (row,) = dataset(1, "p1")
    events = [
        e
        for e in row["events"]
        if e["stage"] not in ("tts_first_byte", "tts_first_text")
    ]
    assert not internal_stage_errors(row, events), "CACHE_HAS_NO_TTS_ENTITY_EVENTS"
    events.append(marker(row, "tts_text_received", "server", 11_650_000_000))
    assert internal_stage_errors(row, events) == ["MISSING_TTS_FIRST_BYTE"]
    events[-1] = marker(row, "fallback_used", "integration", 11_650_000_000, part="tts")
    assert internal_stage_errors(row, events) == ["MISSING_TTS_FIRST_BYTE"]
    events.append(marker(row, "tts_first_byte", "integration", 11_700_000_000))
    assert not internal_stage_errors(row, events)


@pytest.mark.parametrize(
    "part,stage", [("stt", "end_decision"), ("tts", "tts_first_byte")]
)
def test_valid_handover_preserves_before_marker(part, stage):
    row = {"turn_id": "test"}
    events = [
        marker(row, stage, "server", 10),
        marker(row, "fallback_used", "integration", 20, part=part),
        marker(row, stage, "integration", 30),
    ]
    event, errors = owned_internal_event(events, "test", stage, "p1")
    assert not errors, "DOCUMENTED_HANDOVER"
    assert event["source"] == ("server" if part == "tts" else "integration")
    events[0]["t_utc_ns"] = 21
    assert owned_internal_event(events, "test", stage, "p1")[1] == [
        f"WRONG_OWNER_{stage.upper()}"
    ]


@pytest.mark.parametrize("other_device", [False, True])
async def test_expired_or_unrelated_timer_does_not_require_cancellation(
    tmp_path, other_device
):
    hardware = Hardware(arguments(tmp_path))
    hardware.device_id = "input"
    from time import monotonic_ns

    hardware.observe_timer(
        {
            "device_id": "other" if other_device else "input",
            "response_type": "action_done",
            "selected_intent": {"name": "HassStartTimer"},
            "operations": [
                {
                    "service": "timer.started",
                    "arguments": {"seconds": 1},
                    "t_monotonic_ns": monotonic_ns()
                    + (0 if other_device else -2_000_000_000),
                }
            ],
        }
    )
    calls = []

    async def command(kind, **fields):
        calls.append(kind)
        if kind == "get_states":
            return [
                {
                    "entity_id": "conversation.assist_live_short",
                    "attributes": {"virtual_device_id": "virtual"},
                }
            ]
        assert fields["device_id"] == "virtual"
        return {
            "response": {
                "response_type": "action_done",
                "speech_slots": {"canceled": 0},
            }
        }

    await hardware.cancel_timers(command)
    assert calls == ["get_states", "conversation/process"]
