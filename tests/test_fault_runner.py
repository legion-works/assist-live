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
        fallback_deadline_utc_ns=20_000_000_000,
        completed_utc_ns=19_000_000_000,
        failure_cause="server_stopped",
        fault_injected=True,
    )
    return row


def test_partial_transcript_action_fails_gate():
    from tools.fault_runner import acceptable

    assert not acceptable(correct=False, empty=False, action_count=1)


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
        ("completed_utc_ns", 21_000_000_000),
        ("fault_injected", False),
    ],
)
def test_missing_evidence_is_never_empty_safe(field, value):
    from tools.fault_runner import acceptable

    row = trial()
    row.update(recognized_transcript="", action_trace=[])
    row[field] = value
    assert not acceptable(row)


def test_empty_requires_explicit_complete_zero_operations():
    from tools.fault_runner import acceptable

    row = trial()
    row.update(recognized_transcript="", action_trace=[])
    assert acceptable(row)
    row["action_trace"] = trial()["action_trace"]
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
            trials.append(row)
    assert fault_gate({"trials": trials})["passed"]
    trials[0]["stage"] = "speech"
    assert not fault_gate({"trials": trials})["passed"]


@pytest.mark.parametrize("stage", ["before", "speech", "pause", "after-final"])
@pytest.mark.parametrize("evidence", ["missing", "fresh", "stale"])
@pytest.mark.parametrize("late", [False, True])
async def test_driver_injects_each_fault_point_and_restores(
    stage, evidence, late, monkeypatch
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
                        "data": {"runner_data": {"stt_binary_handler_id": 7}},
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

    monkeypatch.setattr(fault_runner, "connect", connect)
    monkeypatch.setattr(fault_runner.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(fault_runner.time, "monotonic", lambda: clock)
    label = {
        **trial()["label"],
        "id": "test",
        "audio_sha256": "a" * 64,
        "split": "held_out",
        "alignment_words": [{"start_sample": 160, "end_sample": 480}],
        "pause_start_sample": 480,
        "pause_end_sample": 800,
    }
    row = await fault_runner.pipeline_trial(
        None,
        Path("unused"),
        "pipeline",
        label,
        bytes(2560),
        stage,
        Server(),
        30,
        device_id="virtual-device" if evidence != "missing" else None,
        turn_sensor="sensor.latency" if evidence != "missing" else None,
    )
    assert calls.count("stop") == calls.count("restore") == 1
    assert calls[-1] == "restore"
    missed = late and stage in ("speech", "pause")
    assert row["fault_injected"] is not missed
    assert row["failure_cause"] == "server_container_stopped"
    assert row["fallback_deadline_utc_ns"] > time.time_ns()
    assert row["acceptable"] is (evidence == "fresh" and not missed)
    if evidence == "missing":
        assert "MISSING_COMPLETE_OPERATION_RECORD" in row["failures"]
    elif evidence == "fresh":
        assert row["turn_id"] == trial()["turn_id"]
        assert row["action_trace"] == trial()["action_trace"]
        assert row["failures"] == (["FAULT_STAGE_MISSED"] if missed else [])
    if stage == "before":
        assert calls.index("stop") < calls.index("run")
    elif stage == "after-final":
        assert calls.index("stop") > calls.index("audio")
    else:
        assert row["fault_audio_sample"] == fault_runner.fault_sample(label, stage)


def test_simulated_trials_never_satisfy_hardware_fault_gate():
    from tools.fault_runner import fault_gate

    trials = [
        dict(trial(), stage=stage, turn_id=f"{stage}-{i}", scope="simulated")
        for stage in ("before", "speech", "pause", "after-final")
        for i in range(10)
    ]
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
        async with temporary_pipeline(command, result, "existing") as values:
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
