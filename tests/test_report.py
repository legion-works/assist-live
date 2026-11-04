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
    snapshot = {
        "observed_utc_ns": 10,
        "integration": {"value": 0, "last_reset_utc_ns": 1},
        **{
            kind: {
                "test_satellite": {
                    "value": 0,
                    "last_reset_utc_ns": 1,
                    **({"boot_id": 0} if kind == "server" else {}),
                }
            }
            for kind in ("server", "device")
        },
    }
    for i in range(offset, offset + count):
        row = copy.deepcopy(sample())
        row.update(
            clip_id=f"clip-{i}",
            turn_id=f"01900000-0000-7000-8000-{i + 1:012x}",
            phase=phase,
            profile="baseline" if phase == "p0" else "safe",
            loss_counters={
                "start": copy.deepcopy(snapshot),
                "end": copy.deepcopy(snapshot),
            },
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


@pytest.mark.parametrize("history", [None, 0, 300, 1000])
def test_report_speech_latency_uses_audio_start_without_changing_mic_open(history):
    from tools.report import group_timings, timing

    _, rows = dataset(1)
    row = rows[0]
    mic = row["mic_open"]
    if history is not None:
        mic["data"]["pre_roll_ms"] = history
    speech = next(item for item in row["events"] if item["stage"] == "speech_start")
    speech["t_utc_ns"] = mic["t_utc_ns"] + (100 - (history or 0)) * 1_000_000
    before = copy.deepcopy(row)
    steps = group_timings([row])["steps"]
    assert steps["mic_to_speech"]["p50_ms"] == 100
    assert steps["wake_to_mic"]["p50_ms"] == 1000
    assert timing(row, "mic_open")["t_utc_ns"] == mic["t_utc_ns"]
    assert steps["mic_to_speech"]["quality"] == {"estimate": 1}
    assert steps["mic_to_speech"]["uncertainty_p90_ms"] == 4
    if history:
        assert steps["mic_to_speech"]["methods"] == {
            "mic_open minus pre_roll_ms;recorded-event": 1
        }
    assert row == before
    assert build(rows, dataset(1)[0])["sample_count"] == 1


@pytest.mark.parametrize("history", [-1, 1001, True, 1.5, "300", None])
@pytest.mark.parametrize("incomplete", [False, True])
def test_report_refuses_invalid_history_before_statistics(history, incomplete):
    from tools.device_replay import ReplayError

    labels, rows = dataset(1)
    rows[0]["mic_open"]["data"]["pre_roll_ms"] = history
    if incomplete:
        rows[0].update(complete=False, failures=["MISSING_DEVICE_EVENT"])
    with pytest.raises(ReplayError, match="INVALID_PRE_ROLL_MS"):
        build(rows, labels)


def build(rows, labels, **kwargs):
    from tools.report import build_report

    kwargs.setdefault("correct_transcripts", transcript_references(labels))
    return build_report(
        rows[0]["phase"], rows, labels, calibration(), observed_rows(), **kwargs
    )


def transcript_references(labels):
    identities = {
        family: {
            "family": family,
            "model": family + "-fixture",
            "code_commit": "a" * 40,
            "model_files": [{"file": "model.bin", "file_sha256": "b" * 64}],
            "settings": "test fixture",
        }
        for family in ("parakeet", "whisper")
    }
    clips = [
        {
            "id": label["id"],
            "audio_sha256": label["audio_sha256"],
            "correct_transcript": label["transcript"],
            "status": "final",
            "source": "engines_agree",
            "engines": [
                {
                    "transcript": label["transcript"],
                    "engine": family,
                }
                for family in ("parakeet", "whisper")
            ],
        }
        for label in labels
    ]
    return [{"kind": "engines", "engine_identities": identities}, *clips]


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
    fault_baselines = {
        engine: {
            "engine_id": "stt." + engine,
            "clip_id": labels[0]["id"],
            "audio_sha256": labels[0]["audio_sha256"],
            "status": "available",
            "transcript": "Turn on test light.",
        }
        for engine in ("server", "fallback")
    }
    faults = {
        "trials": [
            {
                "stage": stage,
                "clip_id": labels[0]["id"],
                "audio_sha256": labels[0]["audio_sha256"],
                "stt_engines": {"server": "stt.server", "fallback": "stt.fallback"},
                "baselines": copy.deepcopy(fault_baselines),
                "turn_id": f"fault-{stage}-{i}",
                "correct_transcript": True,
                "operations_correct": True,
                "action_trace_complete": True,
                "recognized_transcript": baseline_rows[0]["recognized_transcript"],
                "evaluated_intent": baseline_rows[0]["evaluated_intent"],
                "action_trace": baseline_rows[0]["action_trace"],
                "label": {
                    **labels[0],
                    "alignment_words": [{"start_sample": 16000, "end_sample": 32000}],
                    "pause_start_sample": 16000,
                    "pause_end_sample": 32000,
                },
                "fault_injected": True,
                "fault_stopped_audio_sample": 24000,
                "injected_fault": "server_container_stopped",
                "failure_cause": "health_timeout"
                if stage == "before"
                else None
                if stage == "after-final"
                else "stream_closed",
                "fallback_reasons_before": {},
                "fallback_reasons_after": {}
                if stage == "after-final"
                else {"health_timeout" if stage == "before" else "stream_closed": 1},
                "fallback_reasons_delta": {}
                if stage == "after-final"
                else {"health_timeout" if stage == "before" else "stream_closed": 1},
                "fallback_turns": [
                    {
                        "turn_id": f"fault-{stage}-{i}",
                        "destinations": []
                        if stage == "after-final"
                        else [
                            {
                                "part": "stt_fallback",
                                "fallback_reason": "health_timeout"
                                if stage == "before"
                                else "stream_closed",
                            }
                        ],
                    }
                ],
                "events": [{"stage": "run-start", "t_utc_ns": 1_000_000_000}],
                "fallback_deadline_utc_ns": 21_000_000_000,
                "completed_utc_ns": 19_000_000_000,
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
        "p1",
        rows,
        labels,
        cal,
        native,
        baseline=baseline,
        faults=faults,
        correct_transcripts=transcript_references(labels),
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


def test_p1_emitted_word_method_describes_shared_rule(tmp_path, acceptance, capsys):
    import hashlib

    from tools.correct_transcripts import write_data
    from tools.report import main

    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    write_data(tmp_path / "correct_transcripts.jsonl", transcript_references(labels))
    paths = {
        name: tmp_path / (name + ".json")
        for name in (
            "labels",
            "runs",
            "calibration",
            "native",
            "baseline",
            "faults",
            "output",
        )
    }
    for name, records in (("labels", labels), ("runs", rows)):
        paths[name].write_text("".join(json.dumps(row) + "\n" for row in records))
    for name, value in (
        ("calibration", calibration()),
        ("native", observed_rows()),
        ("baseline", baseline),
        ("faults", faults),
    ):
        paths[name].write_text(json.dumps(value))
    (tmp_path / "corpus.sha256").write_text(
        hashlib.sha256(paths["labels"].read_bytes()).hexdigest()
        + "  labels\n"
        + "".join(
            label["audio_sha256"] + "  audio/" + label["audio_path"] + "\n"
            for label in labels
        )
    )
    args = ["p1", "--split", "held_out", "--check"]
    for key, path in paths.items():
        args.extend(["--" + key, str(path)])
    assert main(args) == 0
    emitted = json.loads(paths["output"].read_text())
    assert emitted["accepted"] is True
    assert emitted["methods"]["words"] == (
        "lowercase; punctuation removed; English number words to digits; "
        "Levenshtein distance over those tokens"
    )
    reference_path = tmp_path / "correct_transcripts.jsonl"
    reference_path.write_text(reference_path.read_text() + "\n")
    assert main(args) == 1, "REPORT_ACCEPTED_CHANGED_DATA_WITH_STALE_PIN"
    assert "CORRECT_TRANSCRIPT_DIGEST_MISMATCH" in capsys.readouterr().out


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
    assert result["gates"]["evidence_records"]
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
    from tests.test_fault_runner import empty_trial, set_reasons
    from tools.report import fault_gate

    _, _, _, faults = copy.deepcopy(acceptance)
    assert fault_gate(faults)["passed"]
    previous = faults["trials"][0]
    trial = empty_trial()
    trial.update(
        stage=previous["stage"],
        turn_id=previous["turn_id"],
        action_trace=[{"service": "light.turn_on"}],
    )
    set_reasons(trial, trial["stage"])
    faults["trials"][0] = trial
    assert not fault_gate(faults)["passed"]
    trial["action_trace"] = []
    assert fault_gate(faults)["passed"]
    faults["trials"][1]["turn_id"] = trial["turn_id"]
    assert not fault_gate(faults)["passed"]


def test_p1_cli_uses_the_safe_gate_in_the_acceptance_command(
    tmp_path, acceptance, capsys
):
    import hashlib

    from tools.correct_transcripts import write_data
    from tools.report import main

    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    write_data(tmp_path / "correct_transcripts.jsonl", transcript_references(labels))
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


def test_public_report_cli_shows_qualification_counts_without_private_records(
    tmp_path, acceptance, capsys
):
    import hashlib

    from tools.correct_transcripts import write_data
    from tools.report import main

    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    faults["qualification"] = {
        "candidates": 4,
        "eligible": 1,
        "target": 2,
        "sufficient": False,
        "recordings": [
            {
                "clip_id": "private-recording-sentinel",
                "recognized_transcript": "private transcript sentinel",
                "turn_audio": "private/audio.wav",
                "failure_messages": ["ValueError: private failure sentinel"],
                "eligible": cause is None,
                "cause": cause,
            }
            for cause in [None, "no_wake", "unavailable", "unavailable"]
        ],
    }
    write_data(tmp_path / "correct_transcripts.jsonl", transcript_references(labels))
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
    markdown = tmp_path / "report.md"
    args = ["p1", "--split", "held_out", "--check", "--markdown", str(markdown)]
    for key, path in paths.items():
        args.extend(["--" + key, str(path)])
    assert main(args) == 1
    emitted = paths["output"].read_text()
    assert json.loads(emitted)["faults"].get("qualification") == {
        "candidates": 4,
        "eligible": 1,
        "needed": 2,
        "causes": {"no_wake": 1, "unavailable": 2},
    }, "PUBLIC_QUALIFICATION_COUNTS"
    text = capsys.readouterr().out
    assert "qualification candidates=4 eligible=1 needed=2" in text
    assert '"no_wake": 1' in text and '"unavailable": 2' in text
    assert "Qualification: candidates=4 eligible=1 needed=2" in markdown.read_text()
    for private in (
        "private-recording-sentinel",
        "private transcript sentinel",
        "private/audio.wav",
        "private failure sentinel",
        "failure_messages",
    ):
        assert private not in emitted + text + markdown.read_text(), (
            "NO_PRIVATE_FAULT_DATA"
        )


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


def test_forty_percent_integer_boundary_avoids_float_rounding():
    from tools.report import latency_gate

    baseline = {group: {"n": 1, "p50_ms": 115} for group in ("short", "long")}
    profiles = {
        profile: {group: {"n": 1, "p50_ms": 69} for group in baseline}
        for profile in ("safe", "fast")
    }
    assert latency_gate(baseline, profiles)["passed"]
    profiles["safe"]["long"]["p50_ms"] = 69.000001
    assert not latency_gate(baseline, profiles)["passed"]


def test_last_voice_to_tts_first_byte_keeps_uncertainty_and_missing_count():
    from tools.report import group_timings

    _, rows = dataset(2)
    rows[1]["events"] = [
        event for event in rows[1]["events"] if event["stage"] != "tts_first_byte"
    ]
    step = group_timings(rows)["steps"]["last_voice_to_tts_first_byte"]
    assert step["n"] == step["missing"] == 1
    assert step["p50_ms"] == step["p90_ms"] == 250
    assert step["quality"] == {"estimate": 1}
    assert step["uncertainty_p90_ms"] == 12


@pytest.mark.parametrize("count,passed", [(49, False), (50, True)])
def test_calibration_trial_threshold_is_inclusive(count, passed):
    from tools.report import calibration_ok

    cal = calibration()
    cal["n"] = count
    assert (
        calibration_ok(cal, cal["baseline_config_id"], cal["calibration_revision"])
        is passed
    )


@pytest.mark.parametrize("complete,passed", [(189, False), (190, True)])
def test_completeness_threshold_is_inclusive(complete, passed):
    labels, rows = dataset(200)
    for row in rows[complete:]:
        row.update(
            complete=False,
            failures=["MISSING_OPERATION_EVIDENCE"],
            action_trace=None,
            action_trace_complete=False,
        )
    report = build(rows, labels)
    assert report["gates"]["completeness"] is passed
    assert report["gates"]["evidence_records"]


@pytest.mark.parametrize("count,passed", [(98, False), (100, True)])
def test_reply_group_minimum_is_inclusive(count, passed):
    labels, rows = dataset(count)
    assert build(rows, labels)["gates"]["reply_groups"] is passed


@pytest.mark.parametrize("count,passed", [(199, False), (200, True)])
def test_replay_minimum_is_inclusive(count, passed):
    labels, rows = dataset(count)
    assert build(rows, labels)["gates"]["replay_samples"] is passed


@pytest.mark.parametrize("count,passed", [(29, False), (30, True)])
def test_executable_prefix_minimum_is_inclusive(acceptance, count, passed):
    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    if count == 29:
        labels[229]["executable_prefix"] = False
    report = build(rows, labels, baseline=baseline, faults=faults)
    assert report["gates"]["executable_prefix"] is passed


@pytest.mark.parametrize("count,passed", [(299, False), (300, True)])
def test_pause_minimum_is_inclusive(acceptance, count, passed):
    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    if count == 299:
        rows = [row for row in rows if row["clip_id"] != "clip-499"]
    report = build(rows, labels, baseline=baseline, faults=faults)
    assert report["gates"]["pause_samples"] is passed


@pytest.mark.parametrize("correct,passed", [(189, False), (190, True)])
def test_accuracy_threshold_is_inclusive(acceptance, correct, passed):
    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    for row in rows[correct:200]:
        row["recognized_transcript"] = "wrong command"
    report = build(rows, labels, baseline=baseline, faults=faults)
    assert report["gates"]["accuracy"] is passed


@pytest.mark.parametrize("errors,passed", [(8, True), (9, False)])
def test_word_error_one_percentage_point_boundary_is_inclusive(
    acceptance, errors, passed
):
    labels, rows, baseline, faults = copy.deepcopy(acceptance)
    for row in rows[:errors]:
        row["recognized_transcript"] = "switch on test light"
    report = build(rows, labels, baseline=baseline, faults=faults)
    assert report["word_error_rate"]["words"] == 800
    assert report["gates"]["word_error_rate"] is passed


def test_pause_upper_confidence_boundary_rejects_six_cuts_in_two_hundred():
    from tools.report import pause_gate

    result = pause_gate(6, 200)
    assert result["rate"] == 0.03
    assert 0.06 < result["ci95"][1] < 0.065
    assert not result["passed"]
