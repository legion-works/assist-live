import copy
import json

import pytest

from tests.test_device_replay import event, sample
from tests.test_native_matrix import observed_rows
from tools.device_replay import RUN_FIELDS


def test_incomplete_turns_stay_in_denominator():
    from tools.report import completeness

    assert completeness(complete=190, voice_turns=200)["rate"] == 0.95


def dataset(count=2, phase="p0", latency=500, *, offset=0, set_name="replay"):
    labels, rows = [], []
    for i in range(offset, offset + count):
        row = copy.deepcopy(sample())
        row.update(
            clip_id=f"clip-{i}",
            turn_id=f"01900000-0000-7000-8000-{i + 1:012x}",
            phase=phase,
            profile="baseline" if phase == "p0" else "safe",
        )
        for item in row["events"]:
            item["turn_id"] = row["turn_id"]
        row["events"][2]["t_utc_ns"] = 11_500_000_000 + latency * 1_000_000
        for stage, ms in (
            ("speech_start", 1100),
            ("end_candidate", 1400),
            ("end_decision", 1500),
            ("stt_final", 1600),
            ("tts_first_text", 1700),
            ("tts_first_byte", 1750),
        ):
            row["events"].append(event(stage, ms, row["turn_id"], "integration"))
        row["alignment"]["reply_start_utc_ns"] = row["events"][2]["t_utc_ns"]
        row.update(
            reply_text="x" * (60 if i % 2 == 0 else 61),
            reply_char_count=60 if i % 2 == 0 else 61,
            reply_group="short" if i % 2 == 0 else "long",
        )
        if phase == "p1":
            row["baseline_transcript"] = row["recognized_transcript"]
            row["pause_cut"] = False
        labels.append(
            {
                "id": row["clip_id"],
                "audio_sha256": row["audio_sha256"],
                "audio_path": row["clip_id"] + ".wav",
                "split": "held_out",
                "set": set_name,
                "transcript": row["recognized_transcript"],
                "intent": row["evaluated_intent"],
                "executable_prefix": set_name == "pause" and i - offset < 30,
            }
        )
        rows.append(row)
    return labels, rows


def calibration():
    return {
        "schema_version": 1,
        "n": 50,
        "baseline_config_id": "config-sha256-01",
        "calibration_revision": "calibration-01",
        "latency_gates_accepted": True,
        "proxies": {
            boundary: {"accepted": True, "p90_error_bound_ms": 100}
            for boundary in ("start", "end")
        },
    }


def build(rows, labels, **kwargs):
    from tools.report import build_report

    return build_report(
        rows[0]["phase"], rows, labels, calibration(), observed_rows(), **kwargs
    )


def test_incomplete_rows_and_missing_events_are_not_excluded():
    labels, rows = dataset()
    rows[1]["events"] = [
        item for item in rows[1]["events"] if item["stage"] != "playback_end"
    ]
    rows[1]["playback_end"] = None
    report = build(rows, labels)
    assert report["sample_count"] == 2
    assert report["profiles"]["baseline"]["completeness"]["rate"] == 0.5
    assert report["profiles"]["baseline"]["missing_events"]["playback_end"] == 1
    assert report["exclusions"] == []
    assert not report["accepted"]


def test_linear_interpolation_reply_boundary_quality_and_methods():
    labels, rows = dataset(4)
    rows[2]["alignment"]["last_voice_utc_ns"] -= 100_000_000
    rows[3]["alignment"]["last_voice_utc_ns"] -= 100_000_000
    report = build(rows, labels)
    groups = report["profiles"]["baseline"]["reply_groups"]
    assert groups["short"]["n"] == groups["long"]["n"] == 2
    for group in groups.values():
        step = group["steps"]["last_voice_to_playback"]
        assert step["p50_ms"] == 550
        assert step["p90_ms"] == 590
        assert step["quality"] == {"estimate": 2}
        assert group["steps"]["agent"]["p50_ms"] == 100
        assert group["steps"]["end_decision_to_playback"]["p50_ms"] == 500
        assert group["steps"]["speech_start_to_playback"]["p50_ms"] == 900
    assert report["methods"]["percentile"] == "linear-interpolation-(n-1)*p"
    assert report["methods"]["rate_interval"] == "two-sided Clopper-Pearson 95%"


def test_clopper_pearson_is_two_sided_not_a_normal_approximation():
    from tools.report import completeness, pause_gate

    result = completeness(0, 10)
    assert result["ci95"] == pytest.approx([0, 0.3084971078])
    assert completeness(10, 10)["ci95"] == pytest.approx([0.6915028922, 1])
    assert pause_gate(9, 300)["ci95"][1] == pytest.approx(0.056183543, abs=1e-8)
    assert pause_gate(9, 300)["passed"]
    assert not pause_gate(3, 100)["passed"]
    assert not pause_gate(10, 300)["passed"]
    assert not pause_gate(31, 1000)["passed"]


@pytest.mark.parametrize(
    "field",
    RUN_FIELDS,
)
def test_missing_run_fields_refused_before_any_statistics(field, monkeypatch):
    from tools import report
    from tools.device_replay import ReplayError

    labels, rows = dataset()
    del rows[-1][field]
    monkeypatch.setattr(
        report, "rate", lambda *args: pytest.fail("rate computed before validation")
    )
    monkeypatch.setattr(
        report,
        "word_errors",
        lambda *args: pytest.fail("WER computed before validation"),
    )
    with pytest.raises(ReplayError, match="MISSING_RUN_FIELDS"):
        build(rows, labels)


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("mode", "smoke", "SMOKE"),
        ("frozen", False, "not frozen"),
        ("split", "tuning", "HELD_OUT"),
        ("action_trace_complete", False, "INCOMPLETE"),
        ("audio_sha256", "b" * 64, "HASH"),
        ("baseline_config_id", "other", "MIXED"),
    ],
)
def test_acceptance_refuses_invalid_evidence(field, value, error):
    from tools.device_replay import ReplayError

    labels, rows = dataset()
    rows[-1][field] = value
    with pytest.raises(ReplayError, match=error):
        build(rows, labels)


def test_native_tts_only_and_synthetic_rows_stay_outside_forwarding_denominator():
    labels, rows = dataset(4)
    rows[1]["pipeline"] = "native"
    rows[2]["turn_type"] = "tts_only"
    labels[3]["set"] = "synthetic"
    report = build(rows, labels)
    assert report["sample_count"] == 1
    assert report["profiles"]["baseline"]["completeness"]["denominator"] == 1
    assert report["native_pipeline"]["sample_count"] == 1
    assert report["synthetic"]["sample_count"] == 1
    assert {item["reason"] for item in report["exclusions"]} == {
        "native_pipeline",
        "tts_only",
        "synthetic",
    }


def test_missing_baseline_internal_event_is_an_explicit_proxy():
    labels, rows = dataset()
    rows[0]["events"] = [
        item for item in rows[0]["events"] if item["stage"] != "end_decision"
    ]
    step = build(rows, labels)["profiles"]["baseline"]["reply_groups"]["short"][
        "steps"
    ]["end_decision_to_playback"]
    assert step["quality"] == {"estimate": 1}
    assert any("stt_final proxy" in method for method in step["methods"])
    assert step["p50_ms"] == 400


@pytest.mark.parametrize(
    "safe,fast,passed", [(600, 900, True), (601, 100, False), (600, 100, True)]
)
def test_forty_percent_gate_uses_safe_in_each_nonempty_group(safe, fast, passed):
    from tools.report import latency_gate

    baseline = {group: {"n": 1, "p50_ms": 1000} for group in ("short", "long")}
    profiles = {
        profile: {group: {"n": 1, "p50_ms": value} for group in ("short", "long")}
        for profile, value in (("safe", safe), ("fast", fast))
    }
    assert latency_gate(baseline, profiles)["passed"] is passed
    profiles["safe"]["long"]["n"] = 0
    assert not latency_gate(baseline, profiles)["passed"]


def acceptance_fixture():
    labels, baseline_rows = dataset(200)
    baseline = build(baseline_rows, labels)
    _, safe = dataset(200, "p1", 300)
    pause_labels, pauses = dataset(300, "p1", 300, offset=200, set_name="pause")
    safe += pauses
    fast = copy.deepcopy(safe)
    for row in fast:
        row["profile"] = "fast"
    faults = {
        "trials": [
            {
                "stage": stage,
                "turn_id": f"fault-{stage}-{i}",
                "correct_transcript": True,
                "operations_correct": True,
                "action_trace_complete": True,
                "recognized_transcript": "synthetic command",
                "evaluated_intent": {"name": "HassTurnOn", "slots": {}},
                "action_trace": [],
            }
            for stage in ("before", "speech", "pause", "after-final")
            for i in range(10)
        ]
    }
    return labels + pause_labels, safe + fast, baseline, faults


@pytest.fixture(scope="module")
def acceptance():
    return acceptance_fixture()


def test_p1_known_passing_fixture_and_every_acceptance_gate(acceptance):
    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    result = build(rows, labels, baseline=baseline, faults=faults)
    assert result["accepted"]
    assert all(result["gates"].values())
    assert result["accuracy"]["rate"] == 1
    assert result["wrong_actions"]["count"] == 0
    assert result["word_error_rate"]["rate"] == 0
    assert result["faults"]["count"] == 40
    assert result["pauses"]["denominator"] == 300


@pytest.mark.parametrize(
    "gate",
    [
        "latency",
        "pauses",
        "executable_prefix",
        "accuracy",
        "wrong_actions",
        "word_error_rate",
        "faults",
        "native_matrix",
        "calibration",
        "recording_coverage",
    ],
)
def test_each_p1_gate_has_a_known_failing_fixture(acceptance, gate):
    from tools.report import build_report

    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    native, cal = observed_rows(), calibration()
    if gate == "latency":
        for row in rows:
            if row["profile"] == "safe":
                row["alignment"]["last_voice_utc_ns"] -= 1_000_000
    elif gate == "pauses":
        for row in rows[230:240]:
            row["pause_cut"] = True
    elif gate == "executable_prefix":
        rows[200]["pause_cut"] = True
    elif gate in ("accuracy", "word_error_rate"):
        for row in rows[:11]:
            row["recognized_transcript"] = "incorrect transcript"
    elif gate == "wrong_actions":
        rows[0]["action_trace"].append(copy.deepcopy(rows[0]["action_trace"][0]))
    elif gate == "faults":
        faults["trials"][0]["action_trace_complete"] = False
    elif gate == "native_matrix":
        native[0]["observation"] = None
    elif gate == "calibration":
        cal["proxies"]["start"]["p90_error_bound_ms"] = 101
    else:
        rows.pop(0)
    result = build_report(
        "p1", rows, labels, cal, native, baseline=baseline, faults=faults
    )
    assert not result["gates"][gate]
    assert not result["accepted"]


def test_unavailable_operation_expectation_fails_instead_of_claiming_zero(acceptance):
    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    labels[0]["intent"]["slots"] = {}
    rows[0]["evaluated_intent"]["slots"] = {}
    result = build(rows, labels, baseline=baseline, faults=faults)
    assert result["wrong_actions"]["unavailable"] == 1
    assert not result["gates"]["wrong_actions"]


@pytest.mark.parametrize(
    "change,error",
    [
        ("pair", "PAIRED"),
        ("config", "CONFIGURATION"),
        ("profile", "SAFE"),
        ("pause", "PAUSE"),
        ("baseline", "NOT_ACCEPTED"),
    ],
)
def test_p1_refuses_unpaired_or_missing_gate_evidence_before_output(
    acceptance, change, error
):
    from tools.device_replay import ReplayError

    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    kwargs = {"baseline": baseline, "faults": faults}
    if change == "pair":
        rows[0]["baseline_transcript"] = "different baseline"
    elif change == "config":
        baseline["baseline_config_id"] = "different"
    elif change == "profile":
        kwargs["latency_profile"] = "fast"
    elif change == "pause":
        del rows[200]["pause_cut"]
    else:
        baseline["accepted"] = False
    with pytest.raises(ReplayError, match=error):
        build(rows, labels, **kwargs)


def test_word_error_rate_counts_substitutions_insertions_and_deletions():
    from tools.report import word_errors

    assert word_errors("one two three", "one four") == (2, 3)
    assert word_errors("ONE, two", "one two three") == (1, 2)


def test_cli_check_writes_reports_only_after_validating(tmp_path, capsys):
    from tools.report import main

    labels, rows = dataset(200)
    paths = {
        name: tmp_path / (name + ".json")
        for name in ("labels", "runs", "calibration", "native", "output")
    }
    paths["labels"].write_text("".join(json.dumps(label) + "\n" for label in labels))
    paths["runs"].write_text("".join(json.dumps(row) + "\n" for row in rows))
    paths["calibration"].write_text(json.dumps(calibration()))
    paths["native"].write_text(json.dumps(observed_rows()))
    import hashlib

    (tmp_path / "corpus.sha256").write_text(
        hashlib.sha256(paths["labels"].read_bytes()).hexdigest()
        + "  labels\n"
        + "".join(
            label["audio_sha256"] + "  audio/" + label["audio_path"] + "\n"
            for label in labels
        )
    )
    markdown = tmp_path / "report.md"
    args = ["p0", "--split", "held_out", "--check", "--markdown", str(markdown)]
    for key, path in paths.items():
        args.extend(["--" + key, str(path)])
    assert main(args) == 0
    assert "PASS P0" in capsys.readouterr().out
    assert "last_voice_to_playback" in markdown.read_text()
    paths["output"].unlink()
    del rows[-1]["recognized_transcript"]
    paths["runs"].write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert main(args) == 1
    assert not paths["output"].exists()


def test_explicit_failed_attempt_with_missing_operation_evidence_fails_both_gates(
    acceptance,
):
    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    rows = rows[:2]
    rows[1].update(
        complete=False,
        failures=["MISSING_OPERATION_EVIDENCE"],
        action_trace=None,
        action_trace_complete=False,
    )
    result = build(rows, labels, baseline=baseline, faults=faults)
    assert result["profiles"]["safe"]["completeness"]["denominator"] == 2
    assert result["profiles"]["safe"]["completeness"]["rate"] == 0.5
    assert result["accuracy"]["denominator"] == 2
    assert result["accuracy"]["count"] == 1
    assert result["wrong_actions"]["denominator"] == 2
    assert result["wrong_actions"]["count"] == 1
    assert result["wrong_actions"]["unavailable"] == 1
    assert result["word_error_rate"]["rate"] is None
    assert not result["gates"]["completeness"]
    assert not result["gates"]["wrong_actions"]
    assert not result["accepted"]


def test_failed_attempt_requires_present_fields_and_named_reasons():
    from tools.device_replay import ReplayError

    labels, rows = dataset()
    rows[1].update(
        complete=False, failures=[], action_trace=None, action_trace_complete=False
    )
    with pytest.raises(ReplayError, match="MISSING_FAILURE_REASONS"):
        build(rows, labels)
    rows[1]["failures"] = ["MISSING_OPERATION_EVIDENCE"]
    del rows[1]["action_trace"]
    with pytest.raises(ReplayError, match="MISSING_RUN_FIELDS"):
        build(rows, labels)


def test_fault_empty_path_cannot_hide_operations_and_duplicate_trials(acceptance):
    from tools.report import fault_gate

    _, _, _, faults = copy.deepcopy(acceptance)
    assert fault_gate(faults)["passed"]
    trial = faults["trials"][0]
    trial.update(
        correct_transcript=False,
        empty_transcript=True,
        recognized_transcript="",
        operations=[],
        action_trace=[{"service": "light.turn_on"}],
    )
    assert not fault_gate(faults)["passed"]
    trial["action_trace"] = []
    assert fault_gate(faults)["passed"]
    faults["trials"][1]["turn_id"] = trial["turn_id"]
    assert not fault_gate(faults)["passed"]


def test_p1_cli_uses_the_safe_gate_in_the_acceptance_command(
    tmp_path, acceptance, capsys
):
    import hashlib

    from tools.report import main

    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    paths = {
        key: tmp_path / (key + ".json")
        for key in (
            "labels",
            "runs",
            "baseline",
            "faults",
            "native",
            "calibration",
            "output",
        )
    }
    paths["labels"].write_text("".join(json.dumps(label) + "\n" for label in labels))
    (tmp_path / "corpus.sha256").write_text(
        hashlib.sha256(paths["labels"].read_bytes()).hexdigest()
        + "  labels\n"
        + "".join(
            label["audio_sha256"] + "  audio/" + label["audio_path"] + "\n"
            for label in labels
        )
    )
    for key, value in (
        ("baseline", baseline),
        ("faults", faults),
        ("native", observed_rows()),
        ("calibration", calibration()),
    ):
        paths[key].write_text(json.dumps(value))
    paths["runs"].write_text("".join(json.dumps(row) + "\n" for row in rows))
    args = [
        "p1",
        "--split",
        "held_out",
        "--latency-profile",
        "safe",
        "--check",
        "--markdown",
        str(tmp_path / "acceptance.md"),
    ]
    for key, path in paths.items():
        args.extend(["--" + key, str(path)])
    assert main(args) == 0
    assert "PASS P1 latency" in capsys.readouterr().out
    for row in rows:
        if row["profile"] == "safe":
            row["alignment"]["last_voice_utc_ns"] -= 1_000_000
    paths["runs"].write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert main(args) == 1
    assert "FAIL P1 latency" in capsys.readouterr().out


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("complete", "false", "COMPLETION_FLAG"),
        ("turn_type", "unknown", "TURN_TYPE"),
        ("pipeline", "unknown", "PIPELINE"),
        ("clip_id", "unknown", "UNKNOWN_HELD_OUT"),
    ],
)
def test_report_refuses_invalid_identity_or_completion(field, value, error):
    from tools.device_replay import ReplayError

    labels, rows = dataset()
    rows[1][field] = value
    with pytest.raises(ReplayError, match=error):
        build(rows, labels)


def test_duplicate_recording_profile_is_not_an_extra_sample():
    from tools.device_replay import ReplayError

    labels, rows = dataset()
    with pytest.raises(ReplayError, match="DUPLICATE_RECORDING_PROFILE"):
        build(rows + [rows[0]], labels)
