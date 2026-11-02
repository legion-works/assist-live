import copy

import pytest

from tests.test_calibration import replies
from tests.test_report import acceptance_fixture, build, dataset
from tools.calibrate_playback import calibrate
from tools.device_replay import ReplayError
from tools.report import group_timings, observation_complete, summarize


def test_mixed_sets_refused_before_timing_statistics():
    _, rows = dataset()
    rows[0]["set"], rows[1]["set"] = "replay", "pause"
    with pytest.raises(ReplayError, match="MIXED_CRITERION_SETS"):
        group_timings(rows)


def test_agent_step_names_the_ha_wait():
    _, rows = dataset(1)
    assert group_timings(rows)["steps"]["agent"]["label"] == "agent and HA wait"


def test_pause_rows_cannot_change_replay_latency_or_completeness():
    labels, rows, baseline, faults = acceptance_fixture()
    for row in rows:
        if int(row["clip_id"].split("-")[1]) >= 200:
            row["alignment"]["last_voice_utc_ns"] -= 1_000_000_000
    result = build(rows, labels, baseline=baseline, faults=faults)
    assert result["gates"]["latency"]
    assert result["profiles"]["safe"]["sample_count"] == 200
    assert result["pauses"]["denominator"] == 300
    assert {row["set"] for row in result["rows"]} == {"replay", "pause"}


def test_row_set_cannot_disagree_with_corpus():
    labels, rows = dataset()
    rows[0]["set"] = "pause"
    with pytest.raises(ReplayError, match="RUN_SET_MISMATCH"):
        build(rows, labels)


def test_unknown_estimate_uncertainty_is_not_silently_skipped():
    _, rows = dataset()
    rows[0]["events"][2]["uncertainty_ms"] = None
    step = group_timings(rows)["steps"]["last_voice_to_playback"]
    assert step["uncertainty_p90_ms"] is None
    assert step["uncertainty_unknown"] == 1


def test_host_measured_endpoint_preserves_device_clock_uncertainty():
    _, rows = dataset(1)
    first_byte = next(e for e in rows[0]["events"] if e["stage"] == "tts_first_byte")
    first_byte.update(quality="measured", uncertainty_ms=None)
    step = group_timings(rows)["steps"]["delivery"]
    assert step["uncertainty_p90_ms"] == 2


def test_calibration_adds_device_clock_uncertainty_to_acoustic_uncertainty():
    rows = replies(start_error=95, end_error=95, uncertainty=5)
    for row in rows:
        row["playback_start"]["uncertainty_ms"] = 2
    result = calibrate(rows, "test-device")
    assert result["proxies"]["start"]["p90_error_bound_ms"] == 102
    assert not result["latency_gates_accepted"]


def test_run_loss_disqualifies_every_turn_in_the_run():
    labels, rows = dataset(200)
    rows[0]["loss_present"] = True
    assert not observation_complete(rows[0])
    result = build(rows, labels)
    assert not result["gates"]["completeness"]
    assert result["profiles"]["baseline"]["completeness"]["count"] == 0


def sentence_row():
    _, rows = dataset(1)
    row = rows[0]
    row["events"] = [e for e in row["events"] if e["stage"] != "tts_first_byte"]
    row["events"].extend(
        [
            {
                "turn_id": row["turn_id"],
                "stage": stage,
                "source": "server",
                "t_utc_ns": stamp,
                "quality": "measured",
                "uncertainty_ms": None,
                "data": {"request": 0},
            }
            for stage, stamp in (
                ("tts_text_received", 11_700_000_000),
                ("tts_first_byte", 11_800_000_000),
            )
        ]
    )
    for index, text_ms, first_byte_ms, audio_ms in (
        (0, 0, 100, 200),
        (1, 150, 350, 100),
        (2, 300, 600, 100),
    ):
        row["events"].append(
            {
                "turn_id": row["turn_id"],
                "stage": "tts_sentence",
                "source": "server",
                "data": {
                    "request": 0,
                    "index": index,
                    "text_end": (index + 1) * 10,
                    "text_ms": text_ms,
                    "first_byte_ms": first_byte_ms,
                    "audio_ms": audio_ms,
                },
            }
        )
    row["events"].append(
        {
            "turn_id": row["turn_id"],
            "stage": "tts_last_byte",
            "source": "server",
            "data": {"request": 0, "sentences": 3, "audio_ms": 400},
        }
    )
    return row


def test_sentence_margins_use_first_byte_origin_and_accumulated_stop_time():
    row = sentence_row()
    result = summarize([row])["sentence_timing"][row["turn_id"]][0]
    assert result["margins_ms"] == [0, -50, -200]
    assert result["minimum_margin_ms"] == -200
    assert result["stop_count"] == 2
    assert result["stop_ms"] == 200
    assert result["agent_text_wait_ms"] == [0, 150, 150]
    assert result["provider_ms"] == [100, 200, 300]


def test_absent_sentence_events_are_unavailable_not_zero():
    _, rows = dataset(1)
    assert summarize(rows)["sentence_timing"][rows[0]["turn_id"]] is None


def test_sentence_margin_requires_server_text_and_first_byte_anchors():
    row = sentence_row()
    row["events"] = [e for e in row["events"] if e["stage"] != "tts_text_received"]
    result = summarize([row])["sentence_timing"][row["turn_id"]][0]
    assert result["available"] is False


@pytest.mark.parametrize("missing", ["all", "sentence_only"])
def test_missing_request_uses_only_one_server_stream_without_default_zero(missing):
    from tools.report import sentence_timing

    row = sentence_row()
    for event in row["events"]:
        if event["stage"].startswith("tts_"):
            event["data"]["request"] = 7
            if missing == "all" or event["stage"] == "tts_sentence":
                del event["data"]["request"]
    result = sentence_timing(row)
    assert len(result) == 1
    assert result[0]["available"] is True
    assert result[0]["request"] == (None if missing == "all" else 7)
    assert result[0]["request_method"] == "single_server_stream"
    assert result[0]["margins_ms"] == [0, -50, -200]


@pytest.mark.parametrize("server_first_bytes", [0, 2])
def test_missing_request_refuses_ambiguous_server_streams(server_first_bytes):
    from tools.report import sentence_timing

    row = sentence_row()
    for event in row["events"]:
        event["data"].pop("request", None)
    first = next(e for e in row["events"] if e["stage"] == "tts_first_byte")
    row["events"].remove(first)
    row["events"].extend(copy.deepcopy(first) for _ in range(server_first_bytes))
    assert sentence_timing(row) == [
        {
            "request": None,
            "available": False,
            "reason": "MISSING_TTS_REQUEST_AMBIGUOUS_STREAM",
        }
    ]


def test_test_only_event_cannot_supply_a_measurement_endpoint():
    _, rows = dataset(1)
    row = rows[0]
    row["events"] = [e for e in row["events"] if e["stage"] != "tts_first_byte"]
    injected = copy.deepcopy(row["events"][-1])
    injected.update(stage="tts_first_byte", test_only=True)
    row["events"].append(injected)
    assert group_timings(rows)["steps"]["delivery"]["n"] == 0


@pytest.mark.parametrize("flag", ["loss_unavailable", "reset_unknown"])
def test_unknown_loss_evidence_refuses_run_completeness(flag):
    labels, rows = dataset(200)
    rows[0][flag] = True
    assert not build(rows, labels)["gates"]["completeness"]


def test_missing_counter_snapshots_refuse_completeness():
    labels, rows = dataset(200)
    for row in rows:
        row.pop("loss_counters", None)
    result = build(rows, labels)
    assert not result["gates"]["completeness"]


def test_no_device_events_refuses_completeness_without_claiming_loss():
    labels, rows = dataset(200)
    for snap in rows[0]["loss_counters"].values():
        snap["server"]["test_satellite"].update(boot_id=None, last_reset_utc_ns=None)
    result = build(rows, labels)
    assert not result["gates"]["completeness"]
    assert result["runs"]["test-run"]["loss_present"] is False
    assert result["runs"]["test-run"]["no_device_events"] is True


def test_no_p0_or_p1_criterion_reads_test_pipeline_observations():
    labels, rows, baseline, faults = acceptance_fixture()
    before = build(rows, labels, baseline=baseline, faults=faults)
    for row in rows:
        row["events"].append(
            {"test_only": True, "stage": "unmatched", "turn_id": "test-only-turn"}
        )
        row["test_pipeline"] = {
            "test_only": True,
            "first_agent_text_delta": {"stage": "tts_first_byte", "t_utc_ns": -1},
            "intent_end": None,
            "tool_call_deltas": [{"bad": "evidence"}],
        }
    after = build(rows, labels, baseline=baseline, faults=faults)
    assert before["accepted"] is after["accepted"] is True
    assert before["gates"] == after["gates"]
    assert before["profiles"] == after["profiles"]
