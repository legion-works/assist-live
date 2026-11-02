import copy

import pytest

from tests.test_device_replay import sample


def trial():
    row = sample()
    row.update(
        stage="speech",
        label={
            "transcript": row["recognized_transcript"],
            "intent": row["evaluated_intent"],
        },
        events=[{"stage": "run-start", "t_utc_ns": 1_000_000_000}],
        fallback_deadline_utc_ns=21_000_000_000,
        completed_utc_ns=19_000_000_000,
        injected_fault="server_container_stopped",
        failure_cause="stream_closed",
        fallback_reasons_before={},
        fallback_reasons_after={"stream_closed": 1},
        fallback_reasons_delta={"stream_closed": 1},
        fault_injected=True,
    )
    set_reasons(row, "speech")
    return row


def test_partial_transcript_action_fails_gate():
    from tools.fault_runner import acceptable

    row = trial()
    row["recognized_transcript"] = "turn on"
    assert len(row["action_trace"]) == 1
    assert not acceptable(row)


def test_pipeline_observations_keep_ids_from_intent_end_without_inventing_them():
    from tools.fault_runner import pipeline_observations

    row = trial()
    row["pipeline_request_id"] = 3
    row["events"] = [
        {"stage": "run-start", "data": {}},
        {
            "stage": "intent-end",
            "data": {"intent_output": {"conversation_id": "test-conversation"}},
        },
    ]
    observed = pipeline_observations(row)
    assert observed["conversation_id"] == "test-conversation"
    assert observed["run_id"] is None
    assert observed["intent_end"]["stage"] == "intent-end"


def test_pipeline_observations_accept_events_without_data():
    from tools.fault_runner import pipeline_observations

    row = trial()
    row["pipeline_request_id"] = 3
    row["events"] = [
        {"stage": "run-start", "data": {"run_id": "test-run"}},
        {"stage": "intent-end", "data": {"intent_output": None}},
        {"stage": "intent-progress", "data": None},
        {"stage": "intent-progress", "data": {"chat_log_delta": None}},
        {"stage": "run-end", "data": None},
    ]
    observed = pipeline_observations(row)
    assert observed["run_id"] == "test-run", "events without data keep earlier ids"
    assert observed["conversation_id"] is None
    assert observed["first_agent_text_delta"] is None
    assert observed["tool_call_deltas"] == []
    assert observed["tool_result_deltas"] == []


def test_acceptable_requires_only_a_trial_record():
    import inspect

    from tools.fault_runner import acceptable

    parameters = inspect.signature(acceptable).parameters
    assert list(parameters) == ["trial"]
    assert parameters["trial"].default is inspect.Parameter.empty


def test_classifier_recomputes_transcript_and_operations():
    from tools.fault_runner import acceptable

    row = trial()
    assert acceptable(row)
    row["recognized_transcript"] = "turn on"
    row.update(correct_transcript=True, operations_correct=True)
    assert not acceptable(row)


def test_self_reported_correct_flag_cannot_replace_label_comparison():
    from tools.fault_runner import acceptable

    row = trial()
    row.update(
        recognized_transcript="turn off test light",
        correct_transcript=True,
        operations_correct=True,
    )
    assert not acceptable(row)


@pytest.mark.parametrize(
    "field,value",
    [
        ("recognized_transcript", None),
        ("evaluated_intent", None),
        ("evaluated_intent", {}),
        ("action_trace", None),
        ("action_trace_complete", False),
        ("turn_id", None),
        ("failure_cause", None),
        ("fallback_deadline_utc_ns", None),
        ("completed_utc_ns", 22_000_000_000),
        ("fault_injected", False),
    ],
)
def test_missing_evidence_is_never_empty_safe(field, value):
    from tools.fault_runner import acceptable

    row = trial()
    row.update(recognized_transcript="", action_trace=[])
    row[field] = value
    assert not acceptable(row)


def empty_trial():
    row = trial()
    row.update(
        recognized_transcript=None,
        evaluated_intent=None,
        action_trace=[],
        operations=[],
        agent_records=[],
        pipeline_request_id=3,
    )
    # HA 2026.10.3 default_pipeline.py:422-431,472-486 and run.py:255-273.
    row["events"] = [
        {
            "request_id": 3,
            "stage": stage,
            "source": "ha",
            "data": data,
            "t_utc_ns": 1_000_000_000,
        }
        for stage, data in (
            ("run-start", {}),
            ("stt-start", {"engine": "stt.test"}),
            (
                "error",
                {"code": "stt-no-text-recognized", "message": "No text recognized"},
            ),
            ("run-end", {}),
        )
    ]
    return row


def test_pinned_ha_empty_error_is_safe_without_a_fabricated_intent():
    from tools.fault_runner import acceptable

    row = empty_trial()
    assert acceptable(row)
    row["action_trace"] = trial()["action_trace"]
    assert not acceptable(row)


def test_empty_transcript_with_invented_intent_is_not_evidence():
    from tools.fault_runner import acceptable

    row = trial()
    row.update(recognized_transcript="", action_trace=[])
    assert not acceptable(row)


@pytest.mark.parametrize(
    "change",
    [
        "other-run",
        "other-error",
        "no-end",
        "agent",
        "trace",
        "operations",
        "deadline",
        "missing-operations",
        "final",
        "intent",
        "turn",
        "transcript",
        "intent-record",
        "event-source",
        "missing-agent-log",
        "missing-transcript-field",
        "missing-intent-field",
    ],
)
def test_empty_error_requires_complete_same_run_evidence(change):
    from tools.fault_runner import acceptable

    row = empty_trial()
    if change == "other-run":
        row["events"][2]["request_id"] = 99
    elif change == "other-error":
        row["events"][2]["data"]["code"] = "stt-stream-failed"
    elif change == "no-end":
        row["events"].pop()
    elif change == "agent":
        row["agent_records"] = [{"turn_id": row["turn_id"]}]
    elif change == "trace":
        row["action_trace_complete"] = False
    elif change == "operations":
        row["operations"] = [{"service": "light.turn_on"}]
    elif change == "missing-operations":
        del row["operations"]
    elif change == "final":
        row["events"].insert(
            2, {"request_id": 3, "stage": "stt-end", "source": "ha", "data": {}}
        )
    elif change == "intent":
        row["events"].insert(
            2, {"request_id": 3, "stage": "intent-start", "source": "ha", "data": {}}
        )
    elif change == "turn":
        row["turn_id"] = None
    elif change == "transcript":
        row["recognized_transcript"] = "turn on"
    elif change == "intent-record":
        row["evaluated_intent"] = trial()["evaluated_intent"]
    elif change == "event-source":
        row["events"][2]["source"] = "other"
    elif change == "missing-agent-log":
        del row["agent_records"]
    elif change == "missing-transcript-field":
        del row["recognized_transcript"]
    elif change == "missing-intent-field":
        del row["evaluated_intent"]
    else:
        row["completed_utc_ns"] = row["fallback_deadline_utc_ns"] + 1
    assert not acceptable(row)


def test_correct_transcript_cannot_hide_unlabelled_or_missing_operations():
    from tools.fault_runner import acceptable

    row = trial()
    row["action_trace"] *= 2
    assert not acceptable(row)
    row["action_trace"] = []
    assert not acceptable(row)


def test_fault_gate_requires_all_four_stages_and_distinct_turns():
    from tools.fault_runner import fault_gate

    trials = []
    for stage in ("before", "speech", "pause", "after-final"):
        for i in range(10):
            row = copy.deepcopy(trial())
            row.update(stage=stage, turn_id=f"{stage}-{i}")
            set_reasons(row, stage)
            trials.append(row)
    assert fault_gate({"trials": trials})["passed"]
    trials[0]["stage"] = "speech"
    assert not fault_gate({"trials": trials})["passed"]


def set_reasons(row, stage):
    reason = "health_timeout" if stage == "before" else "stream_closed"
    delta = {} if stage == "after-final" else {reason: 1}
    row.update(
        fallback_reasons_before={},
        fallback_reasons_after=delta.copy(),
        fallback_reasons_delta=delta,
        failure_cause=reason if delta else None,
        fallback_turns=[
            {
                "turn_id": row["turn_id"],
                "destinations": [{"part": "stt_fallback", "fallback_reason": reason}]
                if delta
                else [],
            }
        ],
    )


@pytest.mark.parametrize("stage", ["before", "speech", "pause", "after-final"])
@pytest.mark.parametrize(
    "delta", ["correct", "wrong", "two-reasons", "two-counts", "none", "unreadable"]
)
def test_recorded_reason_delta_must_match_stage(stage, delta):
    from tools.fault_runner import acceptable

    row = trial()
    row["stage"] = stage
    set_reasons(row, stage)
    if delta == "wrong":
        row["fallback_reasons_after"] = {"not_local_only": 1}
    elif delta == "two-reasons":
        row["fallback_reasons_after"] = {"health_timeout": 1, "stream_closed": 1}
    elif delta == "two-counts":
        row["fallback_reasons_after"] = {"stream_closed": 2}
    elif delta == "none":
        row["fallback_reasons_after"] = {}
    elif delta == "unreadable":
        row["fallback_reasons_before"] = None
    if delta not in ("correct", "unreadable"):
        row["fallback_reasons_delta"] = row["fallback_reasons_after"].copy()
        row["failure_cause"] = next(iter(row["fallback_reasons_delta"]), None)
        row["fallback_turns"][0]["destinations"] = (
            [{"part": "stt_fallback", "fallback_reason": row["failure_cause"]}]
            if row["fallback_reasons_delta"]
            else []
        )
    assert acceptable(row) is (
        delta == "correct" or (stage == "after-final" and delta == "none")
    )


@pytest.mark.parametrize("stage", ["before", "speech", "pause"])
@pytest.mark.parametrize("source", ["foreign-turn", "tts-only", "matching", "mismatch"])
def test_reason_belongs_to_trial_turn_and_stt_part(stage, source):
    from tools.fault_runner import acceptable

    row = trial()
    row["stage"] = stage
    set_reasons(row, stage)
    row["fallback_turns"] = [
        {
            "turn_id": "foreign-turn" if source == "foreign-turn" else row["turn_id"],
            "destinations": [
                {
                    "part": "tts_fallback" if source == "tts-only" else "stt_fallback",
                    "fallback_reason": "open_error"
                    if source == "mismatch"
                    else row["failure_cause"],
                }
            ],
        }
    ]
    assert acceptable(row) is (source == "matching")


@pytest.mark.parametrize("elapsed,passed", [(19.9, True), (20.0, True), (20.1, False)])
def test_deadline_is_twenty_seconds_from_run_start(elapsed, passed):
    from tools.fault_runner import acceptable

    row = trial()
    row["fault_requested_utc_ns"] = 10_000_000_000
    row["completed_utc_ns"] = 1_000_000_000 + int(elapsed * 1e9)
    assert acceptable(row) is passed
    row["fallback_deadline_utc_ns"] = 30_000_000_000
    assert not acceptable(row)


def test_stop_plan_targets_first_quarter_and_subtracts_latency():
    from tools.fault_runner import fault_sample

    label = {"alignment_words": [{"start_sample": 16000, "end_sample": 32000}]}
    assert fault_sample(label, "speech") == 20000
    assert fault_sample(label, "speech", stop_latency_seconds=0.1) == 18400


@pytest.mark.parametrize(
    "stage,reason",
    [
        ("before", reason)
        for reason in (
            "health_timeout",
            "health_error",
            "server_unhealthy",
            "open_error",
        )
    ]
    + [
        (stage, reason)
        for stage in ("speech", "pause")
        for reason in ("stream_closed", "message_timeout", "stream_error")
    ],
)
def test_each_allowed_recorded_reason_has_one_count(stage, reason):
    from tools.fault_runner import acceptable

    row = trial()
    row.update(
        stage=stage,
        failure_cause=reason,
        fallback_reasons_before={reason: 4},
        fallback_reasons_after={reason: 5},
        fallback_reasons_delta={reason: 1},
    )
    row["fallback_turns"][0]["destinations"][0]["fallback_reason"] = reason
    assert acceptable(row)


@pytest.mark.parametrize(
    "after",
    [
        None,
        [],
        {"stream_closed": True},
        {"stream_closed": 1.5},
        {"stream_closed": -1},
        {},
    ],
)
def test_unreadable_or_reset_diagnostics_cannot_be_reason_evidence(after):
    from tools.fault_runner import acceptable

    row = trial()
    row.update(
        fallback_reasons_before={"stream_closed": 4}, fallback_reasons_after=after
    )
    assert not acceptable(row)


def test_check_cannot_override_spec_deadline(tmp_path, monkeypatch):
    from tools import fault_runner

    monkeypatch.setattr(fault_runner, "native_main", lambda _: 0)
    with pytest.raises(SystemExit) as error:
        fault_runner.main(
            [
                "--native-matrix",
                "unused",
                "--output",
                str(tmp_path / "result.json"),
                "--check",
                "--fallback-deadline",
                "30",
            ]
        )
    assert error.value.code == 2


@pytest.mark.parametrize("stage", ["before", "speech", "pause", "after-final"])
@pytest.mark.parametrize("evidence", ["missing", "fresh", "stale"])
@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("reason_source", ["matching", "mismatch"])
async def test_driver_injects_each_fault_point_and_restores(
    stage, evidence, late, reason_source, monkeypatch
):
    import asyncio
    import time
    from pathlib import Path

    from tools import fault_runner

    calls = []
    sent = asyncio.Event()
    original_sleep = asyncio.sleep
    clock = time.monotonic()

    async def fast_sleep(delay):
        nonlocal clock
        await original_sleep(0)
        clock += max(delay, 0)

    class Server:
        async def stop(self):
            nonlocal clock
            calls.append("stop")
            clock += 0.1
            if late:
                clock += 1

        async def restore(self):
            calls.append("restore")

    class Socket:
        def __init__(self):
            self.index = 0
            self.last_id = 0
            self.messages = [
                {"success": True},
                {
                    "id": 3,
                    "type": "event",
                    "event": {
                        "type": "run-start",
                        "data": {
                            "runner_data": {"stt_binary_handler_id": 7},
                            "run_id": "ha-test-run",
                            "conversation_id": "test-conversation",
                        },
                    },
                },
                {
                    "id": 3,
                    "type": "event",
                    "event": {
                        "type": "stt-end",
                        "data": {"stt_output": {"text": "turn on test light"}},
                    },
                },
                {"id": 3, "type": "event", "event": {"type": "run-end"}},
                {"id": 4, "type": "result", "success": True},
            ]
            if evidence != "missing":
                row = trial()
                self.messages.insert(
                    3,
                    {
                        "id": 2,
                        "type": "event",
                        "event": {
                            "event_type": "assist_live_test_record",
                            "data": {
                                "recognized_transcript": row["recognized_transcript"],
                                "selected_intent": row["evaluated_intent"],
                                "completed": True,
                                "device_id": "virtual-device",
                                "turn_id": None,
                                "reply": "Done.",
                                "operations": [
                                    {
                                        "service": "light.turn_on",
                                        "target": "light.virtual_light",
                                        "arguments": {},
                                        "t_monotonic_ns": 123,
                                    }
                                ],
                            },
                        },
                    },
                )
                self.messages.insert(
                    0,
                    {
                        "id": 1,
                        "type": "result",
                        "success": True,
                        "result": [
                            {
                                "entity_id": "sensor.latency",
                                "attributes": {
                                    "turn_id": "previous",
                                    "turn_type": "voice",
                                },
                            }
                        ],
                    },
                )
                self.messages[-1]["result"] = [
                    {
                        "entity_id": "sensor.latency",
                        "attributes": {
                            "turn_id": row["turn_id"]
                            if evidence == "fresh"
                            else "previous",
                            "turn_type": "voice",
                        },
                    }
                ]
            for delta in reversed(
                [
                    {"role": "tool", "content": "Not agent text"},
                    {"role": "assistant", "content": "First text"},
                    {"tool_calls": [{"id": "call-1", "tool_name": "test_tool"}]},
                    {"role": "tool", "tool_call_id": "call-1", "tool_result": {}},
                ]
            ):
                self.messages.insert(
                    3,
                    {
                        "id": 3,
                        "type": "event",
                        "event": {
                            "type": "intent-progress",
                            "data": {"chat_log_delta": delta},
                        },
                    },
                )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_json(self, message):
            assert message["id"] > self.last_id, "HA requires increasing command IDs"
            self.last_id = message["id"]
            if message.get("type") == "assist_pipeline/run":
                calls.append("run")

        async def send_bytes(self, data):
            calls.append("audio")
            if len(data) == 1:
                sent.set()

        async def receive_json(self):
            if self.index == (2 if evidence == "missing" else 3):
                await sent.wait()
            if self.index == 3:
                await original_sleep(0)
            response = self.messages[self.index]
            self.index += 1
            return response

    async def connect(*args):
        return Socket()

    async def snapshot(*args):
        calls.append("diagnostics")
        first = calls.count("diagnostics") == 1
        counts = (
            {"health_timeout": 4, "stream_closed": 2}
            if first
            else {
                "health_timeout": 5 if stage == "before" else 4,
                "stream_closed": 3 if stage in ("speech", "pause") else 2,
            }
        )
        reason = "health_timeout" if stage == "before" else "stream_closed"
        if reason_source == "mismatch":
            reason = "open_error" if stage == "before" else "message_timeout"
        return {
            "fallback_reasons": counts,
            "turns": [
                {
                    "turn_id": trial()["turn_id"],
                    "destinations": [
                        {"part": "stt_fallback", "fallback_reason": reason}
                    ]
                    if not first and stage != "after-final"
                    else [],
                }
            ],
        }

    monkeypatch.setattr(fault_runner, "connect", connect)
    monkeypatch.setattr(fault_runner, "fallback_snapshot", snapshot)
    monkeypatch.setattr(fault_runner.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(fault_runner.time, "monotonic", lambda: clock)
    label = {
        **trial()["label"],
        "id": "test",
        "audio_sha256": "a" * 64,
        "split": "held_out",
        "alignment_words": [{"start_sample": 16000, "end_sample": 32000}],
        "pause_start_sample": 32000,
        "pause_end_sample": 48000,
    }
    row = await fault_runner.pipeline_trial(
        None,
        Path("unused"),
        "pipeline",
        label,
        bytes(112000),
        stage,
        Server(),
        stop_latency_seconds=0.1,
        device_id="virtual-device" if evidence != "missing" else None,
        turn_sensor="sensor.latency" if evidence != "missing" else None,
    )
    assert calls.count("stop") == calls.count("restore") == 1
    assert calls[-1] == "restore"
    missed = late and stage in ("speech", "pause")
    assert row["fault_injected"] is not missed
    assert calls[0] == "diagnostics"
    assert calls[-2] == "diagnostics"
    assert row["injected_fault"] == "server_container_stopped"
    assert row["failure_cause"] == (
        None
        if evidence != "fresh" or stage == "after-final"
        else "open_error"
        if reason_source == "mismatch" and stage == "before"
        else "message_timeout"
        if reason_source == "mismatch"
        else "health_timeout"
        if stage == "before"
        else "stream_closed"
    )
    assert row["fallback_deadline_utc_ns"] > time.time_ns()
    start = next(event for event in row["events"] if event["stage"] == "run-start")
    assert all(item["test_only"] is True for item in row["events"])
    assert all(item["turn_id"] == row["turn_id"] for item in row["events"])
    assert row["pipeline_request_id"] == 3
    assert row["test_pipeline"]["run_id"] == "ha-test-run"
    assert row["test_pipeline"]["conversation_id"] == "test-conversation"
    assert (
        row["test_pipeline"]["first_agent_text_delta"]["data"]["chat_log_delta"][
            "content"
        ]
        == "First text"
    )
    assert len(row["test_pipeline"]["tool_call_deltas"]) == 1
    assert len(row["test_pipeline"]["tool_result_deltas"]) == 1
    assert row["test_pipeline"]["tts_start"] == "not_run"
    assert row["loss_counters"]["start"]["observed_utc_ns"] > 0
    assert row["loss_counters"]["end"]["observed_utc_ns"] > 0
    assert row["fallback_deadline_utc_ns"] == start["t_utc_ns"] + 20_000_000_000
    mismatch = reason_source == "mismatch" and stage != "after-final"
    assert row["acceptable"] is (evidence == "fresh" and not missed and not mismatch)
    if evidence == "missing":
        assert "MISSING_COMPLETE_OPERATION_RECORD" in row["failures"]
    elif evidence == "fresh":
        assert row["turn_id"] == trial()["turn_id"]
        assert row["action_trace"] == trial()["action_trace"]
        assert row["failures"] == (
            (["FAULT_STAGE_MISSED"] if missed else [])
            + (["MISSING_OR_UNEXPECTED_FALLBACK_REASON"] if mismatch else [])
        )
    if stage == "before":
        assert calls.index("stop") < calls.index("run")
    elif stage == "after-final":
        assert calls.index("stop") > calls.index("audio")
    else:
        assert row["fault_planned_audio_sample"] == (
            18400 if stage == "speech" else 34400
        )
        assert (
            row["fault_planned_audio_sample"]
            <= row["fault_audio_sample"]
            < (row["fault_planned_audio_sample"] + 320)
        )
        if not missed:
            assert row["fault_stopped_audio_sample"] == pytest.approx(
                20160 if stage == "speech" else 36160, abs=1
            )


def test_simulated_trials_never_satisfy_hardware_fault_gate():
    from tools.fault_runner import fault_gate

    trials = [
        dict(trial(), stage=stage, turn_id=f"{stage}-{i}", scope="simulated")
        for stage in ("before", "speech", "pause", "after-final")
        for i in range(10)
    ]
    for row in trials:
        set_reasons(row, row["stage"])
    result = fault_gate({"trials": trials})
    assert result["count"] == 40
    assert not result["passed"]


def test_native_cli_delegates_the_unchanged_matrix(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from tools.fault_runner import main
    from tools.native_matrix import run_native_matrix

    monkeypatch.delenv("ASSIST_LIVE_NATIVE_OBSERVATIONS", raising=False)
    matrix = Path(__file__).resolve().parents[1] / "docs/testing/native-functions.md"
    output = tmp_path / "native.json"
    assert (
        main(["--native-matrix", str(matrix), "--output", str(output), "--check"]) == 1
    )
    assert json.loads(output.read_text()) == run_native_matrix()


def test_audio_input_checks_hash_before_decoding(tmp_path):
    import hashlib
    import wave

    from tools.fault_runner import audio_input

    path = tmp_path / "synthetic.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(640))
    label = {
        "audio_path": path.name,
        "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    assert audio_input(label, tmp_path) == bytes(640)
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        audio_input(label, tmp_path)


@pytest.mark.parametrize("fail", [False, True])
async def test_temporary_pipeline_is_unassigned_and_deleted_on_every_exit(fail):
    from tools.fault_runner import temporary_pipeline
    from tools.probe_pipelines import pipeline_fields

    source = {"id": "existing", **pipeline_fields("stt.test", "tts.test", "test-voice")}
    pipelines = [source]
    calls = []
    states = [
        {
            "entity_id": "select.satellite_pipeline",
            "state": "existing",
            "attributes": {"options": ["existing"]},
        },
        {
            "entity_id": "conversation.assist_live_short",
            "attributes": {"virtual_device_id": "virtual-device"},
        },
        {
            "entity_id": "sensor.latency",
            "attributes": {"turn_type": "voice", "turn_id": "previous"},
        },
    ]

    async def command(kind, **fields):
        calls.append((kind, fields))
        if kind == "assist_pipeline/pipeline/list":
            return copy.deepcopy(
                {"pipelines": pipelines, "preferred_pipeline": "existing"}
            )
        if kind == "get_states":
            return copy.deepcopy(states)
        if kind == "config/entity_registry/list":
            return [
                {"entity_id": "stt.test", "device_id": "stt-device"},
                {
                    "entity_id": "sensor.latency",
                    "device_id": "stt-device",
                    "platform": "assist_live",
                },
            ]
        if kind == "assist_pipeline/pipeline/create":
            created = {"id": "temporary", **fields}
            pipelines.append(created)
            return created
        if kind == "assist_pipeline/pipeline/delete":
            pipelines.pop()
            return None
        pytest.fail(kind)

    result = {}
    try:
        async with temporary_pipeline(
            command, result, "existing", cleanup_command=command
        ) as values:
            assert values == ("temporary", "virtual-device", "sensor.latency")
            assert (
                pipelines[-1]["conversation_engine"] == "conversation.assist_live_short"
            )
            assert pipelines[-1]["stt_engine"] == source["stt_engine"]
            assert pipelines[-1]["tts_engine"] == source["tts_engine"]
            assert pipelines[-1]["tts_voice"] == source["tts_voice"]
            assert pipelines[-1]["prefer_local_intents"] is False
            if fail:
                raise ValueError("synthetic trial failure")
    except ValueError as error:
        assert fail and str(error) == "synthetic trial failure"
    assert result["bench_restored"] is True
    assert result["bench_before"] == result["bench_after"]
    assert pipelines == [source]
    assert all(
        kind not in ("call_service", "assist_pipeline/pipeline/set_preferred")
        for kind, _ in calls
    )


@pytest.mark.parametrize("pause_row", [False, True])
async def test_missing_pause_boundaries_refuse_before_any_live_mutation(
    tmp_path, monkeypatch, pause_row
):
    import argparse

    from tools import fault_runner

    label = {
        **trial()["label"],
        "id": "test",
        "split": "held_out",
        "set": "pause" if pause_row else "replay",
        "alignment_words": [{"start_sample": 160, "end_sample": 480}],
    }
    monkeypatch.setattr(fault_runner, "load_corpus", lambda _: [label])
    monkeypatch.setattr(fault_runner, "audio_input", lambda *_: bytes(640))
    monkeypatch.setattr(
        fault_runner,
        "TestServer",
        lambda: pytest.fail("live mutation before preflight"),
    )
    args = argparse.Namespace(
        simulate=True,
        check=True,
        output=tmp_path / "result.json",
        labels=tmp_path / "unused.jsonl",
        native_matrix=None,
        stages="before,speech,pause,after-final",
        each=2,
        audio_root=tmp_path,
    )
    result = await fault_runner.run(args)
    assert result["preflight_failed"] is True
    assert result["trials"] == []
    assert result["stage_status"]["pause"]["status"] == "NOT RUN"
    assert (
        "boundary" in result["stage_status"]["pause"]["reason"]
        or "boundaries" in result["stage_status"]["pause"]["reason"]
    )
    assert "bench_before" not in result


def test_smoke_can_select_three_stages_but_check_still_requires_full_acceptance(
    tmp_path, monkeypatch
):
    from tools import fault_runner

    seen = []

    async def run(args):
        seen.append(args.stages)
        return {
            "trials": [
                dict(trial(), stage=stage, turn_id=f"{stage}-{i}")
                for stage in args.stages.split(",")
                for i in range(2)
            ]
        }

    monkeypatch.setattr(fault_runner, "run", run)
    args = [
        "--simulate",
        "--stages",
        "before,speech,after-final",
        "--each",
        "2",
        "--token-file",
        "unused",
        "--pipeline",
        "test",
        "--audio-root",
        str(tmp_path),
        "--output",
        str(tmp_path / "result.json"),
        "--check",
    ]
    assert fault_runner.main(args) == 1
    assert seen == ["before,speech,after-final"]


def test_result_denominators_never_substitute_virtual_for_device_rows(
    tmp_path, monkeypatch
):
    import json

    from tools.fault_runner import write_result
    from tools.native_matrix import run_native_matrix

    monkeypatch.delenv("ASSIST_LIVE_NATIVE_OBSERVATIONS", raising=False)
    result = {
        "trials": [dict(trial(), scope="simulated")],
        "native_device": run_native_matrix(),
        "native_virtual": [{"passed": True}, {"passed": False}],
    }
    output = tmp_path / "result.json"
    write_result(output, result)
    saved = json.loads(output.read_text())
    assert saved["faults"]["count"] == saved["faults"]["denominator"] == 1
    assert not saved["faults"]["passed"]
    assert saved["native_denominators"]["device"] == {
        "count": 0,
        "denominator": 32,
        "measure": "observed rows",
    }
    assert saved["native_denominators"]["virtual"] == {
        "count": 1,
        "denominator": 2,
        "measure": "satisfactory rows",
    }


async def test_driver_records_the_pinned_empty_error_as_complete_zero_operations(
    monkeypatch,
):
    from pathlib import Path

    from tools import fault_runner

    expected = empty_trial()

    class Server:
        async def stop(self):
            pass

        async def restore(self):
            pass

    class Socket:
        def __init__(self):
            self.messages = iter(
                [
                    {
                        "success": True,
                        "result": [
                            {
                                "entity_id": "sensor.latency",
                                "attributes": {"turn_id": "previous"},
                            }
                        ],
                    },
                    {"success": True},
                    *[
                        {
                            "id": 3,
                            "type": "event",
                            "event": {
                                "type": item["stage"],
                                "data": {
                                    **item["data"],
                                    **(
                                        {"runner_data": {"stt_binary_handler_id": 7}}
                                        if item["stage"] == "run-start"
                                        else {}
                                    ),
                                },
                            },
                        }
                        for item in expected["events"]
                    ],
                    {
                        "id": 4,
                        "type": "result",
                        "success": True,
                        "result": [
                            {
                                "entity_id": "sensor.latency",
                                "attributes": {
                                    "turn_id": expected["turn_id"],
                                    "turn_type": "voice",
                                },
                            }
                        ],
                    },
                ]
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_json(self, message):
            pass

        async def send_bytes(self, message):
            pass

        async def receive_json(self):
            return next(self.messages)

    async def connect(*args):
        return Socket()

    snapshots = iter(
        [
            {"fallback_reasons": {}, "turns": []},
            {
                "fallback_reasons": {"health_timeout": 1},
                "turns": [
                    {
                        "turn_id": expected["turn_id"],
                        "destinations": [
                            {
                                "part": "stt_fallback",
                                "fallback_reason": "health_timeout",
                            }
                        ],
                    }
                ],
            },
        ]
    )

    async def snapshot(*args):
        return next(snapshots)

    monkeypatch.setattr(fault_runner, "connect", connect)
    monkeypatch.setattr(fault_runner, "fallback_snapshot", snapshot)
    label = {
        **expected["label"],
        "id": "test",
        "audio_sha256": "a" * 64,
        "split": "held_out",
    }
    row = await fault_runner.pipeline_trial(
        None,
        Path("unused"),
        "pipeline",
        label,
        bytes(640),
        "before",
        Server(),
        turn_sensor="sensor.latency",
    )
    assert row["acceptable"]
    assert row["recognized_transcript"] is row["evaluated_intent"] is None
    assert row["action_trace_complete"] is True
    assert row["action_trace"] == row["operations"] == row["agent_records"] == []
    assert all(
        item["request_id"] == row["pipeline_request_id"] for item in row["events"]
    )


@pytest.fixture
def cleanup_bench():
    from tools.probe_pipelines import pipeline_fields

    bench = {
        "pipelines": [{"id": "existing", **pipeline_fields("stt.test", "tts.test")}],
        "calls": [],
        "delete_attempts": 0,
        "delete": None,
    }

    async def command(kind, **fields):
        bench["calls"].append((kind, fields))
        if kind == "assist_pipeline/pipeline/list":
            return copy.deepcopy(
                {"pipelines": bench["pipelines"], "preferred_pipeline": "existing"}
            )
        if kind == "get_states":
            return [
                {
                    "entity_id": "conversation.assist_live_short",
                    "attributes": {"virtual_device_id": "virtual"},
                },
                {"entity_id": "sensor.latency", "attributes": {"turn_type": "voice"}},
            ]
        if kind == "config/entity_registry/list":
            return [
                {"entity_id": "stt.test", "device_id": "device"},
                {
                    "entity_id": "sensor.latency",
                    "device_id": "device",
                    "platform": "assist_live",
                },
            ]
        if kind == "assist_pipeline/pipeline/create":
            created = {"id": "temporary", **fields}
            bench["pipelines"].append(created)
            return created
        if kind == "assist_pipeline/pipeline/delete":
            bench["delete_attempts"] += 1
            if bench["delete"]:
                await bench["delete"]()
            bench["pipelines"][:] = [
                row for row in bench["pipelines"] if row["id"] != fields["pipeline_id"]
            ]
            return None
        pytest.fail(kind)

    bench["command"] = command
    return bench


async def test_cancel_during_run_and_delete_waits_for_shielded_cleanup(cleanup_bench):
    import asyncio

    from tools.fault_runner import temporary_pipeline

    running, deleting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    result = {}

    async def delete():
        deleting.set()
        await release.wait()

    cleanup_bench["delete"] = delete

    async def run():
        async with temporary_pipeline(
            cleanup_bench["command"],
            result,
            "existing",
            cleanup_command=cleanup_bench["command"],
        ):
            running.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await asyncio.wait_for(running.wait(), 1)
    task.cancel()
    await asyncio.wait_for(deleting.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_bench["delete_attempts"] == 1
    assert result["bench_restored"] is True
    assert result["bench_before"] == result["bench_after"]
    assert [row["id"] for row in cleanup_bench["pipelines"]] == ["existing"]


@pytest.mark.parametrize("persistent", [False, True])
async def test_delete_timeout_has_own_deadline_one_retry_and_named_failure(
    cleanup_bench, monkeypatch, persistent
):
    import asyncio

    from tools import fault_runner

    monkeypatch.setattr(fault_runner, "PIPELINE_CLEANUP_TIMEOUT", 0.01, raising=False)

    async def delete():
        if persistent or cleanup_bench["delete_attempts"] == 1:
            await asyncio.sleep(0.05)

    cleanup_bench["delete"] = delete
    result = {}
    if persistent:
        with pytest.raises(
            ValueError, match="TEMPORARY_PIPELINE_DELETE_FAILED: temporary"
        ):
            async with fault_runner.temporary_pipeline(
                cleanup_bench["command"],
                result,
                "existing",
                cleanup_command=cleanup_bench["command"],
            ):
                pass
        assert "temporary" in result["cleanup_error"]
        assert result["bench_restored"] is False
        assert "bench_after" in result
    else:
        async with fault_runner.temporary_pipeline(
            cleanup_bench["command"],
            result,
            "existing",
            cleanup_command=cleanup_bench["command"],
        ):
            pass
        assert result["bench_restored"] is True
    assert cleanup_bench["delete_attempts"] == 2


async def test_leftover_temporary_pipeline_is_named_and_not_deleted(cleanup_bench):
    from tools.fault_runner import temporary_pipeline

    cleanup_bench["pipelines"].append(
        {"id": "leftover", "name": "assist-live f11-smoke"}
    )
    with pytest.raises(ValueError, match="TEMPORARY_PIPELINE_EXISTS: leftover"):
        async with temporary_pipeline(
            cleanup_bench["command"],
            {},
            "existing",
            cleanup_command=cleanup_bench["command"],
        ):
            pytest.fail("ran with a leftover pipeline")
    assert not any(
        kind.endswith(("/create", "/delete")) for kind, _ in cleanup_bench["calls"]
    )


def test_native_checked_output_can_be_in_repo_reports_but_simulation_cannot(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from tools import fault_runner

    monkeypatch.setattr(fault_runner, "ROOT", tmp_path, raising=False)
    reports = tmp_path / "docs/reports"
    reports.mkdir(parents=True)
    matrix = Path(__file__).resolve().parents[1] / "docs/testing/native-functions.md"
    output = reports / "native.json"
    base = ["--native-matrix", str(matrix), "--output", str(output)]
    assert fault_runner.main([*base, "--simulate", "--check"]) == 1
    assert not output.exists()
    assert fault_runner.main(base) == 1
    assert not output.exists()
    assert fault_runner.main([*base, "--check"]) == 1
    assert output.exists()


def test_report_and_fault_runner_share_one_rate_owner():
    from tools import fault_runner, report
    from tools.metrics import rate

    assert report.rate is fault_runner.rate is rate
    assert report.fault_gate is fault_runner.fault_gate
    assert rate.__module__ == "tools.metrics"
    assert rate(1, 2)["rate"] == 0.5


@pytest.mark.parametrize(
    "code", ["TEMPORARY_PIPELINE_DELETE_FAILED", "TEMPORARY_PIPELINE_EXISTS"]
)
def test_cleanup_and_leftover_errors_print_pipeline_id_and_exit_nonzero(
    tmp_path, monkeypatch, capsys, code
):
    from tools import fault_runner

    async def run(args):
        raise fault_runner.TemporaryPipelineError(code + ": temporary-id")

    monkeypatch.setattr(fault_runner, "run", run)
    assert (
        fault_runner.main(
            [
                "--simulate",
                "--token-file",
                "unused",
                "--pipeline",
                "test",
                "--audio-root",
                str(tmp_path),
                "--output",
                str(tmp_path / "result.json"),
            ]
        )
        == 1
    )
    assert code + ": temporary-id" in capsys.readouterr().out


@pytest.mark.parametrize(
    "simulated,check,location,allowed",
    [
        (False, True, "docs/reports/trial.json", True),
        (False, False, "docs/reports/trial.json", False),
        (True, True, "docs/reports/trial.json", False),
        (True, False, "docs/reports/trial.json", False),
        (False, True, "tools/trial.json", False),
    ],
)
def test_in_repo_output_policy_is_explicit(
    tmp_path, monkeypatch, simulated, check, location, allowed
):
    from tools import fault_runner

    monkeypatch.setattr(fault_runner, "ROOT", tmp_path)
    if allowed:
        fault_runner.validate_output(
            tmp_path / location, simulated=simulated, check=check
        )
    else:
        with pytest.raises(ValueError):
            fault_runner.validate_output(
                tmp_path / location, simulated=simulated, check=check
            )
