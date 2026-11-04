import copy
import json
from argparse import Namespace

import pytest

from tests.test_fault_runner import set_reasons, trial
from tests.test_report import acceptance_fixture, build
from tools import fault_runner


@pytest.fixture
def attempt_run(tmp_path, monkeypatch):
    label = {
        **trial()["label"],
        "id": "test",
        "audio_sha256": "a" * 64,
        "split": "held_out",
        "set": "pause",
        "alignment_words": [{"start_sample": 16000, "end_sample": 48000}],
        "pause_start_sample": 48000,
        "pause_end_sample": 80000,
    }
    calls = []
    monkeypatch.setattr(fault_runner, "load_corpus", lambda _: [label])
    monkeypatch.setattr(fault_runner, "audio_input", lambda *_: b"synthetic")

    async def restore(*_):
        pass

    async def measure(*_):
        return 0.1

    async def engines(*_):
        return trial()["stt_engines"]

    async def baselines(*_, **__):
        return trial()["baselines"]

    monkeypatch.setattr(fault_runner.TestServer, "restore", restore)
    monkeypatch.setattr(fault_runner.TestServer, "measure_stop_latency", measure)
    monkeypatch.setattr(fault_runner, "baseline_engines", engines)
    monkeypatch.setattr(fault_runner, "collect_baselines", baselines)

    async def run(outcomes, *, stage="speech", each=1):
        async def pipeline(*args, **kwargs):
            index = len(calls)
            assert index < 6, "ATTEMPT_CAP_THREE"
            position, text, latency = outcomes[min(index, len(outcomes) - 1)]
            calls.append((args, kwargs))
            window = fault_runner.fault_window(label, stage)
            injected = window is None or window[0] <= position < window[1]
            row = trial()
            row.update(
                label=label,
                stage=stage,
                turn_id=f"attempt-{index}",
                recognized_transcript=text,
                fault_injected=injected,
                fault_stopped_audio_sample=position,
                measured_stop_latency_seconds=latency,
                fault_planned_audio_sample=fault_runner.fault_sample(
                    label,
                    stage,
                    stop_latency_seconds=kwargs["stop_latency_seconds"],
                    stop_margin_seconds=kwargs["stop_margin_seconds"],
                ),
                failures=[] if injected else ["FAULT_STAGE_MISSED"],
            )
            set_reasons(row, stage)
            row["fault_stopped_audio_sample"] = position
            row["status"] = fault_runner.trial_status(row)
            row["acceptable"] = row["status"] == "passed"
            return row

        monkeypatch.setattr(fault_runner, "pipeline_trial", pipeline)
        output = tmp_path / "result.json"
        result = await fault_runner.run(
            Namespace(
                output=output,
                simulate=True,
                check=True,
                labels=tmp_path / "labels.jsonl",
                native_matrix=None,
                stages=stage,
                each=each,
                audio_root=tmp_path,
                token_file=tmp_path / "authentication.json",
                temporary_agent_pipeline=False,
                pipeline="existing",
            )
        )
        assert json.loads(output.read_text()) == result, "ATTEMPTS_PERSISTED"
        return result, calls

    return run


@pytest.mark.parametrize("stage,inside", [("speech", 32000), ("pause", 64000)])
@pytest.mark.parametrize("position", [0, 90000])
@pytest.mark.parametrize("correct", [True, False], ids=["correct", "wrong"])
async def test_early_and_late_misses_replace_same_slot_without_outcome_selection(
    attempt_run, stage, inside, position, correct
):
    text = trial()["recognized_transcript"]
    result, calls = await attempt_run(
        [(position, text if correct else "wrong command", 0.3), (inside, text, 0.2)],
        stage=stage,
    )
    assert len(calls) == 2, "OUTCOME_BLIND_REPLACEMENT"
    assert len(result["trials"]) == 1, "ONE_COUNTED_SLOT"
    assert len(result["attempts"]) == 2, "ALL_ATTEMPTS_RETAINED"
    missed, counted = result["attempts"]
    assert missed["status"] == "invalid_injection", "MISSED_NOT_FAILED"
    assert missed["recognized_transcript"] == (text if correct else "wrong command"), (
        "MISSED_RECORDS_RETAINED"
    )
    assert missed["trial_slot"] == counted["trial_slot"] == 1
    assert [missed["attempt"], counted["attempt"]] == [1, 2]
    assert [call[0][3]["id"] for call in calls] == ["test", "test"], "SAME_CLIP"
    assert result["trials"][0] == counted
    assert counted["status"] == "passed"
    assert result["faults"]["denominator"] == 1, "INVALID_NOT_IN_DENOMINATOR"
    assert result["faults"]["stages"][stage] == {
        "counted_trials": 1,
        "passed": 1,
        "failed": 0,
        "unavailable": 0,
        "invalid_attempts": 1,
    }, "INVALID_NOT_FAILED_IN_SUMMARY"


async def test_three_misses_exhaust_slot_without_counting_failures(attempt_run):
    result, calls = await attempt_run([(90000, "wrong command", 0.1)])
    assert len(calls) == 3, "ATTEMPT_CAP_THREE"
    assert len(result["attempts"]) == 3
    assert all(row["status"] == "invalid_injection" for row in result["attempts"])
    assert result["trials"][0]["status"] == "unavailable", "EXHAUSTED_UNAVAILABLE"
    assert not result["faults"]["passed"], "UNAVAILABLE_FAILS_GATE"
    assert result["faults"]["stages"]["speech"] == {
        "counted_trials": 0,
        "passed": 0,
        "failed": 0,
        "unavailable": 1,
        "invalid_attempts": 3,
    }, "INVALID_NOT_FAILED_IN_SUMMARY"


async def test_attempt_samples_change_next_plan_and_keep_prior_snapshot(attempt_run):
    text = trial()["recognized_transcript"]
    result, calls = await attempt_run(
        [(90000, text, 0.3), (64000, text, 0.5)], stage="pause"
    )
    first, second = result["attempts"]
    assert first["stop_latency_samples_seconds"] == [0.1], "PLAN_SAMPLE_SNAPSHOT"
    assert second["stop_latency_samples_seconds"] == [0.1, 0.3], "ATTEMPT_ACCUMULATED"
    assert result["stop_latency_samples_seconds"] == [0.1, 0.3, 0.5]
    assert calls[1][1]["stop_latency_seconds"] == pytest.approx(0.2)
    assert calls[1][1]["stop_margin_seconds"] == pytest.approx(0.07)
    assert first["fault_planned_audio_sample"] == 54400
    assert second["fault_planned_audio_sample"] == 52800, "NEXT_PLAN_USES_ALL_SAMPLES"


async def test_speech_samples_continue_across_counted_slots(attempt_run):
    text = trial()["recognized_transcript"]
    result, calls = await attempt_run([(32000, text, 0.3)], each=6)
    assert len(calls) == 6
    assert result["attempts"][0]["stop_latency_samples_seconds"] == [0.1] * 5
    assert (
        result["attempts"][-1]["stop_latency_samples_seconds"] == [0.1] * 5 + [0.3] * 5
    )
    assert result["attempts"][-1]["fault_planned_audio_sample"] == 28800
    assert result["stop_latency_samples_seconds"] == [0.1] * 5 + [0.3] * 6


async def test_exhausted_slot_does_not_use_next_slot_attempt_budget(attempt_run):
    text = trial()["recognized_transcript"]
    result, calls = await attempt_run(
        [(90000, text, 0.1)] * 3 + [(32000, text, 0.1)], each=2
    )
    assert len(calls) == 4, "SLOT_BUDGET_RESETS"
    assert [row["status"] for row in result["trials"]] == ["unavailable", "passed"]
    assert [row["attempt"] for row in result["attempts"]] == [1, 2, 3, 1]


@pytest.mark.parametrize("stage", ["before", "after-final"])
async def test_unwindowed_stages_and_valid_failures_are_not_replaced(
    attempt_run, stage
):
    result, calls = await attempt_run([(90000, "wrong command", 0.3)], stage=stage)
    assert len(calls) == 1, "UNWINDOWED_NEVER_REPLACED"
    assert result["trials"][0]["status"] == "failed"
    assert result["faults"]["stages"][stage]["invalid_attempts"] == 0


async def test_verified_wrong_transcript_is_failed_without_replacement(attempt_run):
    result, calls = await attempt_run([(32000, "wrong command", 0.3)])
    assert len(calls) == 1, "VALID_FAILURE_NEVER_REPLACED"
    assert result["faults"]["stages"]["speech"]["failed"] == 1


def test_report_counts_slots_and_shows_invalid_attempts_separately():
    labels, rows, baseline, faults = copy.deepcopy(acceptance_fixture())
    missed = copy.deepcopy(faults["trials"][10])
    missed.update(
        status="invalid_injection",
        fault_injected=False,
        fault_stopped_audio_sample=90000,
        label={"alignment_words": [{"start_sample": 16000, "end_sample": 48000}]},
    )
    faults["attempts"] = [missed, *copy.deepcopy(faults["trials"])]
    result = build(rows, labels, baseline=baseline, faults=faults)
    assert result["faults"]["count"] == 40, "REPORT_COUNTS_SLOTS"
    assert result["faults"]["denominator"] == 40
    assert result["faults"]["passed"]
    assert result["faults"]["stages"]["speech"] == {
        "counted_trials": 10,
        "passed": 10,
        "failed": 0,
        "unavailable": 0,
        "invalid_attempts": 1,
    }, "REPORT_SHOWS_INVALID_ATTEMPTS"


def test_counted_window_requires_actual_sample_not_claimed_injection():
    row = trial()
    row["label"]["alignment_words"] = [{"start_sample": 16000, "end_sample": 48000}]
    row.pop("fault_stopped_audio_sample", None)
    assert not fault_runner.acceptable(row), "COUNTED_INJECTION_NEEDS_SAMPLE"
    row["fault_stopped_audio_sample"] = 48000
    row["fault_window_samples"] = [0, 90000]
    assert not fault_runner.acceptable(row), "LABEL_WINDOW_IS_AUTHORITATIVE"
