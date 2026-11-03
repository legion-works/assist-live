import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.calibrate_playback import percentile
from tools.corpus import load_corpus
from tools.correct_transcripts import check_digest, final_text, split_records
from tools.correct_transcripts import validate as validate_transcripts
from tools.device_replay import (
    DEVICE_STAGES,
    RUN_FIELDS,
    ReplayError,
    frozen_corpus,
    pre_roll_ms,
    sample_errors,
)
from tools.fault_runner import fault_gate
from tools.measurement import LOSS_FLAGS, endpoint_uncertainty, loss_window
from tools.metrics import rate
from tools.native_matrix import validate_native_matrix
from tools.result_text import words
from tools.test_environment import intent_matches, validate_intent_action_evidence

Row = dict[str, Any]
REQUIRED = RUN_FIELDS
STEPS = {
    "wake_to_mic": ("wake_detected", "mic_open"),
    "mic_to_speech": ("turn_audio_start", "speech_start"),
    "speech_to_candidate": ("speech_start", "end_candidate"),
    "candidate_to_decision": ("end_candidate", "end_decision"),
    "decision_to_final": ("end_decision", "stt_final"),
    "agent": ("stt_final", "tts_first_text"),
    "tts": ("tts_first_text", "tts_first_byte"),
    "delivery": ("tts_first_byte", "playback_start"),
    "playback": ("playback_start", "playback_end"),
    "end_decision_to_playback": ("end_decision", "playback_start"),
    "speech_start_to_playback": ("speech_start", "playback_start"),
    "last_voice_to_playback": ("last_voice", "playback_start"),
    "last_voice_to_tts_first_byte": ("last_voice", "tts_first_byte"),
}


def completeness(complete: int, voice_turns: int) -> Row:
    return rate(complete, voice_turns)


def pause_gate(cuts: int, samples: int) -> Row:
    result = rate(cuts, samples)
    result["passed"] = bool(
        samples > 0 and result["rate"] <= 0.03 and result["ci95"][1] <= 0.06
    )
    return result


def latency_gate(baseline: Row, profiles: Row) -> Row:
    results = {}
    for profile in ("safe", "fast"):
        groups = {}
        for group in ("short", "long"):
            before = baseline.get(group, {})
            after = profiles.get(profile, {}).get(group, {})
            valid = (
                before.get("n", 0) > 0
                and after.get("n", 0) > 0
                and before.get("p50_ms", 0) > 0
                and after.get("p50_ms") is not None
            )
            reduction = 1 - after["p50_ms"] / before["p50_ms"] if valid else None
            groups[group] = {
                "reduction": reduction,
                "passed": bool(valid and after["p50_ms"] * 5 <= before["p50_ms"] * 3),
            }
        results[profile] = {
            "groups": groups,
            "passed": all(item["passed"] for item in groups.values()),
        }
    return {"profiles": results, "profile": "safe", "passed": results["safe"]["passed"]}


def word_errors(reference: str, hypothesis: str) -> tuple[int, int]:
    expected, actual = words(reference), words(hypothesis)
    if not expected:
        raise ReplayError("EMPTY_REFERENCE_TRANSCRIPT")
    previous = list(range(len(actual) + 1))
    for i, token in enumerate(expected, 1):
        current = [i]
        for j, other in enumerate(actual, 1):
            current.append(
                min(
                    current[-1] + 1, previous[j] + 1, previous[j - 1] + (token != other)
                )
            )
        previous = current
    return previous[-1], len(expected)


def validate_rows(rows: list[Row], labels: list[Row], phase: str) -> None:
    by_id = {label["id"]: label for label in labels}
    seen = set()
    configs = set()
    revisions = set()
    for row in rows:
        missing = [key for key in REQUIRED if key not in row]
        if missing:
            raise ReplayError("MISSING_RUN_FIELDS: " + ",".join(missing))
        errors = sample_errors(row, allow_incomplete=True)
        # Missing observations reduce completeness, not the denominator.
        allowed = {"MISSING_DEVICE_" + stage.upper() for stage in DEVICE_STAGES}
        allowed.add("UNMATCHED_TURN")
        if row["alignment"] is None:
            allowed.add("MISSING_ALIGNMENT")
        errors = [error for error in errors if error not in allowed]
        if errors:
            raise ReplayError("INVALID_RUN: " + ",".join(errors))
        if row["phase"] != phase or row["split"] != "held_out":
            raise ReplayError("HELD_OUT_PHASE_REQUIRED")
        if row.get("turn_type", "voice") not in ("voice", "tts_only"):
            raise ReplayError("INVALID_TURN_TYPE")
        if row.get("pipeline", "forwarding") not in ("forwarding", "native"):
            raise ReplayError("INVALID_PIPELINE")
        key = (
            row["clip_id"],
            row["profile"],
            row.get("pipeline", "forwarding"),
            row.get("turn_type", "voice"),
        )
        if key in seen:
            raise ReplayError("DUPLICATE_RECORDING_PROFILE")
        seen.add(key)
        label = by_id.get(row["clip_id"])
        if label is None or label.get("split") != "held_out":
            raise ReplayError("UNKNOWN_HELD_OUT_RECORDING")
        if label.get("audio_sha256") != row["audio_sha256"]:
            raise ReplayError("UNRESOLVED_RECORDING_HASH")
        if "set" in row and row["set"] != label.get("set"):
            raise ReplayError("RUN_SET_MISMATCH")
        configs.add(row["baseline_config_id"])
        revisions.add(row["calibration_revision"])
    if not rows or len(configs) != 1 or len(revisions) != 1:
        raise ReplayError("EMPTY_OR_MIXED_CONFIGURATION")


def timing(row: Row, stage: str) -> Row | None:
    if stage == "turn_audio_start":
        mic = timing(row, "mic_open")
        if mic is None:
            return None
        history = pre_roll_ms(mic)
        return {
            **mic,
            "t_utc_ns": mic["t_utc_ns"] - history * 1_000_000,
            "method": "mic_open minus pre_roll_ms" if history else "recorded-event",
        }
    if stage == "last_voice":
        if row["alignment"] is None:
            return None
        return {
            "t_utc_ns": row["alignment"]["last_voice_utc_ns"]
            + row["server_clock"]["offset_ns"],
            "quality": "estimate",
            "uncertainty_ms": row["alignment"]["uncertainty_ms"],
        }
    events = [
        event
        for event in row["events"]
        if event["stage"] == stage
        and event["turn_id"] == row["turn_id"]
        and not event.get("test_only")
    ]
    if stage in DEVICE_STAGES:
        events = [event for event in events if event["source"] == "device"]
    if len(events) == 1:
        result = dict(events[0])
        if result.get("data", {}).get("decision_source") == "ha_audio_end":
            result["quality"] = "proxy"
        return result
    if stage == "end_decision" and row["phase"] == "p0":
        proxy = timing(row, "stt_final")
        if proxy:
            return {
                **proxy,
                "quality": "proxy",
                "method": "stt_final proxy for unavailable baseline end_decision",
            }
    return None


def evidence_complete(row: Row) -> bool:
    return (
        isinstance(row["recognized_transcript"], str)
        and bool(row["recognized_transcript"].strip())
        and isinstance(row["evaluated_intent"], dict)
        and isinstance(row["action_trace"], list)
        and row["action_trace_complete"] is True
    )


def group_timings(rows: list[Row]) -> Row:
    sets = {row.get("set", "replay") for row in rows}
    if len(sets) > 1:
        raise ReplayError("MIXED_CRITERION_SETS")
    result = {}
    for name, (start, end) in STEPS.items():
        values, uncertainties = [], []
        qualities: Counter[str] = Counter()
        methods: Counter[str] = Counter()
        unknown = 0
        for row in rows:
            left, right = timing(row, start), timing(row, end)
            if left is None or right is None:
                continue
            elapsed = (right["t_utc_ns"] - left["t_utc_ns"]) / 1e6
            if elapsed < 0:
                raise ReplayError("NEGATIVE_TIMING_STEP: " + name)
            values.append(elapsed)
            quality = (
                "estimate"
                if "estimate" in (left["quality"], right["quality"])
                else "proxy"
                if "proxy" in (left["quality"], right["quality"])
                else "measured"
            )
            qualities[quality] += 1
            methods[
                left.get("method", "recorded-event")
                + ";"
                + right.get("method", "recorded-event")
            ] += 1
            left_uncertainty = endpoint_uncertainty(left)
            right_uncertainty = endpoint_uncertainty(right)
            if left_uncertainty is None or right_uncertainty is None:
                unknown += 1
            else:
                uncertainties.append(left_uncertainty + right_uncertainty)
        result[name] = {
            "label": "agent and HA wait" if name == "agent" else name,
            "n": len(values),
            "missing": len(rows) - len(values),
            "p50_ms": percentile(values, 50) if values else None,
            "p90_ms": percentile(values, 90) if values else None,
            "quality": dict(qualities),
            "methods": dict(methods),
            "uncertainty_p90_ms": percentile(uncertainties, 90)
            if uncertainties and not unknown
            else None,
            "uncertainty_unknown": unknown,
        }
    return {"n": len(rows), "steps": result}


def observation_complete(row: Row) -> bool:
    return (
        row.get("complete") is not False
        and not row.get("loss_present")
        and not row.get("loss_unavailable")
        and not row.get("reset_unknown")
        and not row.get("no_device_events")
        and all(timing(row, stage) is not None for stage in DEVICE_STAGES)
        and not any(
            event["stage"] == "unmatched" or event["turn_id"] != row["turn_id"]
            for event in row["events"]
        )
    )


def sentence_timing(row: Row) -> list[Row] | None:
    events = [
        event
        for event in row["events"]
        if event.get("turn_id") == row["turn_id"]
        and event["stage"]
        in ("tts_sentence", "tts_last_byte", "tts_text_received", "tts_first_byte")
        and not event.get("test_only")
    ]
    if not any(e["stage"] in ("tts_sentence", "tts_last_byte") for e in events):
        return None
    missing_request = any(event["data"].get("request") is None for event in events)
    requests = {
        event["data"]["request"]
        for event in events
        if event["data"].get("request") is not None
    }
    streams: list[tuple[int | None, list[Row]]] = []
    if missing_request:
        server_first = [
            e
            for e in events
            if e["stage"] == "tts_first_byte" and e["source"] == "server"
        ]
        if len(server_first) != 1 or len(requests) > 1:
            return [
                {
                    "request": None,
                    "available": False,
                    "reason": "MISSING_TTS_REQUEST_AMBIGUOUS_STREAM",
                }
            ]
        streams.append(
            (
                next(iter(requests), None),
                [
                    e
                    for e in events
                    if e["stage"] not in ("tts_text_received", "tts_first_byte")
                    or e["source"] == "server"
                ],
            )
        )
    else:
        streams = [
            (request, [e for e in events if e["data"]["request"] == request])
            for request in sorted(requests)
        ]
    result: list[Row] = []
    for request, stream in streams:
        sentences = sorted(
            [e["data"] for e in stream if e["stage"] == "tts_sentence"],
            key=lambda data: data["index"],
        )
        endings = [e["data"] for e in stream if e["stage"] == "tts_last_byte"]
        text = [e for e in stream if e["stage"] == "tts_text_received"]
        first = [e for e in stream if e["stage"] == "tts_first_byte"]
        if not sentences or len(endings) != 1 or len(text) != 1 or len(first) != 1:
            result.append({"request": request, "available": False})
            continue
        if [s["index"] for s in sentences] != list(range(len(sentences))):
            raise ReplayError("INVALID_SENTENCE_INDICES")
        ending = endings[0]
        if ending["sentences"] != len(sentences):
            result.append({"request": request, "available": False, "truncated": True})
            continue
        origin = (first[0]["t_utc_ns"] - text[0]["t_utc_ns"]) / 1e6
        audio = stop_ms = previous_text = 0.0
        stops = 0
        margins, waits, provider = [], [], []
        for sentence in sentences:
            margin = audio - (sentence["first_byte_ms"] - origin)
            margins.append(margin)
            gap = max(0.0, -margin - stop_ms)
            if gap:
                stops += 1
                stop_ms += gap
            waits.append(sentence["text_ms"] - previous_text)
            provider.append(sentence["first_byte_ms"] - sentence["text_ms"])
            previous_text = sentence["text_ms"]
            audio += sentence["audio_ms"]
        result.append(
            {
                "request": request,
                "request_method": "single_server_stream"
                if missing_request
                else "recorded",
                "available": True,
                "sentences": ending["sentences"],
                "audio_ms": ending["audio_ms"],
                "margins_ms": margins,
                "minimum_margin_ms": min(margins),
                "stop_count": stops,
                "stop_ms": stop_ms,
                "agent_text_wait_ms": waits,
                "provider_ms": provider,
                "provider_measure": (
                    "provider time including queue behind prior sentence"
                ),
                "playback_model": "server first byte, not observed device stops",
                "sentence_time_origin": "server tts_text_received",
            }
        )
    return result


def summarize(rows: list[Row]) -> Row:
    missing: Counter[str] = Counter()
    for row in rows:
        for stage in DEVICE_STAGES:
            if timing(row, stage) is None:
                missing[stage] += 1
        missing["unmatched"] += sum(
            event["stage"] == "unmatched" or event["turn_id"] != row["turn_id"]
            for event in row["events"]
        )
    complete = sum(observation_complete(row) for row in rows)
    groups = {
        group: group_timings(
            [
                row
                for row in rows
                if (
                    ("short" if len(row["reply_text"]) <= 60 else "long")
                    if isinstance(row["reply_text"], str)
                    else row["reply_group"]
                )
                == group
            ]
        )
        for group in ("short", "long")
    }
    return {
        "sample_count": len(rows),
        "completeness": completeness(complete, len(rows)),
        "missing_events": dict(missing),
        "failed_turns": [
            {
                "clip_id": row["clip_id"],
                "turn_id": row["turn_id"],
                "reasons": row.get("failures", []),
            }
            for row in rows
            if row.get("complete") is False
        ],
        "reply_groups": groups,
        "sentence_timing": {row["turn_id"]: sentence_timing(row) for row in rows},
        "unclassified_reply_count": sum(
            not isinstance(row["reply_text"], str)
            and row["reply_group"] not in ("short", "long")
            for row in rows
        ),
    }


def calibration_ok(calibration: Row, config: str, revision: str) -> bool:
    return bool(
        calibration.get("schema_version") == 1
        and calibration.get("n", 0) >= 50
        and calibration.get("baseline_config_id") == config
        and calibration.get("calibration_revision") == revision
        and calibration.get("latency_gates_accepted") is True
        and all(
            calibration.get("proxies", {}).get(boundary, {}).get("accepted") is True
            and calibration["proxies"][boundary].get("p90_error_bound_ms", 101) <= 100
            for boundary in ("start", "end")
        )
    )


def operation_matches(row: Row, label: Row) -> bool | None:
    if not evidence_complete(row):
        return None
    actual = [
        {
            "service": action["service"],
            "target": action["virtual_target"],
            "arguments": action["arguments"],
        }
        for action in row["action_trace"]
    ]
    failures = validate_intent_action_evidence(
        {"selected_intent": row["evaluated_intent"], "operations": actual}, label
    )
    return None if "operation expectation unavailable" in failures else not failures


def build_report(
    phase: str,
    rows: list[Row],
    labels: list[Row],
    calibration: Row,
    native: list[Row],
    *,
    baseline: Row | None = None,
    faults: Row | None = None,
    latency_profile: str = "safe",
    correct_transcripts: list[Row] | None = None,
) -> Row:
    if latency_profile != "safe":
        raise ReplayError("DEFAULT_SAFE_PROFILE_REQUIRED")
    rows = [
        {
            **row,
            "events": [
                event
                for event in row["events"]
                if not isinstance(event, dict) or not event.get("test_only")
            ],
        }
        if isinstance(row.get("events"), list)
        else row
        for row in rows
    ]
    validate_rows(rows, labels, phase)
    by_id = {label["id"]: label for label in labels}
    if correct_transcripts is not None:
        transcript_errors = validate_transcripts(correct_transcripts, labels)
        if transcript_errors:
            raise ReplayError(
                "INVALID_CORRECT_TRANSCRIPTS: " + "; ".join(transcript_errors)
            )
    references = {
        row["id"]: final_text(row)
        for row in (
            split_records(correct_transcripts)[1] if correct_transcripts else []
        )
    }
    rows = [{**row, "set": by_id[row["clip_id"]]["set"]} for row in rows]
    for row in rows:
        counters = row.get("loss_counters", {})
        start, end = counters.get("start"), counters.get("end")
        if isinstance(start, dict) and isinstance(end, dict):
            observed_loss = loss_window(start, end)
            for flag in LOSS_FLAGS:
                row[flag] = bool(row.get(flag) or observed_loss[flag])
        else:
            row["loss_unavailable"] = True
    run_flags: dict[str, Row] = {}
    for row in rows:
        flags = run_flags.setdefault(row["run_id"], dict.fromkeys(LOSS_FLAGS, False))
        for flag in LOSS_FLAGS:
            flags[flag] = flags[flag] or bool(row.get(flag))
    rows = [{**row, **run_flags[row["run_id"]]} for row in rows]
    excluded = []
    primary, native_rows = [], []
    synthetic = []
    for row in rows:
        reason = (
            "tts_only"
            if row.get("turn_type", "voice") == "tts_only"
            else "synthetic"
            if by_id[row["clip_id"]].get("set") == "synthetic"
            else None
        )
        if reason:
            excluded.append({"turn_id": row["turn_id"], "reason": reason})
            if reason == "synthetic":
                synthetic.append(row)
        elif row.get("pipeline", "forwarding") == "native":
            native_rows.append(row)
            excluded.append({"turn_id": row["turn_id"], "reason": "native_pipeline"})
        else:
            primary.append(row)
    profiles = {
        profile: summarize(
            [
                row
                for row in primary
                if row["profile"] == profile and row["set"] == "replay"
            ]
        )
        for profile in (("baseline",) if phase == "p0" else ("safe", "fast"))
    }
    config, revision = rows[0]["baseline_config_id"], rows[0]["calibration_revision"]
    native_errors = validate_native_matrix(native)
    result = {
        "schema_version": 1,
        "phase": phase,
        "baseline_config_id": config,
        "calibration_revision": revision,
        "sample_count": len(primary),
        "input_count": len(rows),
        "exclusions": excluded,
        "profiles": profiles,
        "rows": [
            {"turn_id": row["turn_id"], "set": row["set"], "run_id": row["run_id"]}
            for row in rows
        ],
        "runs": {
            row["run_id"]: {
                "loss_counters": row.get("loss_counters"),
                **run_flags[row["run_id"]],
            }
            for row in rows
        },
        "native_pipeline": summarize(native_rows),
        "synthetic": summarize(synthetic),
        "methods": {
            "percentile": "linear-interpolation-(n-1)*p",
            "rate_interval": "two-sided Clopper-Pearson 95%",
            "words": (
                "lowercase; punctuation removed; English number words to digits; "
                "Levenshtein distance over those tokens"
            ),
        },
        "native_errors": native_errors,
    }
    gates = {
        "calibration": calibration_ok(calibration, config, revision),
        "native_matrix": not native_errors,
        "completeness": all(
            profile["completeness"]["rate"] is not None
            and profile["completeness"]["rate"] >= 0.95
            for profile in profiles.values()
        ),
        "reply_groups": all(
            all(group["n"] >= 50 for group in profile["reply_groups"].values())
            for profile in profiles.values()
        ),
    }
    # Missing recording rows cannot silently reduce an acceptance denominator.
    expected = {
        label["id"]
        for label in labels
        if label.get("split") == "held_out" and label.get("set") == "replay"
    }
    gates["recording_coverage"] = bool(expected) and all(
        {
            row["clip_id"]
            for row in primary
            if row["profile"] == profile
            and by_id[row["clip_id"]].get("set") == "replay"
        }
        == expected
        for profile in profiles
    )
    gates["replay_samples"] = len(expected) >= 200
    gates["evidence_records"] = bool(primary) and all(
        evidence_complete(row) for row in primary if observation_complete(row)
    )
    if phase == "p1":
        if (
            baseline is None
            or baseline.get("schema_version") != 1
            or baseline.get("phase") != "p0"
        ):
            raise ReplayError("P0_BASELINE_REQUIRED")
        if (
            baseline.get("baseline_config_id") != config
            or baseline.get("calibration_revision") != revision
        ):
            raise ReplayError("BASELINE_CONFIGURATION_MISMATCH")
        if not baseline.get("accepted"):
            raise ReplayError("P0_BASELINE_NOT_ACCEPTED")
        safe = [row for row in primary if row["profile"] == "safe"]
        replay = [row for row in safe if by_id[row["clip_id"]].get("set") == "replay"]
        pauses = [row for row in safe if by_id[row["clip_id"]].get("set") == "pause"]
        cuts = []
        for row in pauses:
            if (
                type(row.get("pause_cut")) is not bool
                and row.get("complete") is not False
            ):
                raise ReplayError("MISSING_PAUSE_OBSERVATION")
            cuts.append(row.get("pause_cut", True))
        pause = pause_gate(sum(cuts), len(cuts))
        prefix = [
            row for row in pauses if by_id[row["clip_id"]].get("executable_prefix")
        ]
        prefix_result = rate(
            sum(row.get("pause_cut", True) for row in prefix), len(prefix)
        )
        correct = sum(
            evidence_complete(row)
            and references.get(row["clip_id"]) is not None
            and words(row["recognized_transcript"])
            == words(references[row["clip_id"]] or "")
            and intent_matches(row["evaluated_intent"], by_id[row["clip_id"]]["intent"])
            for row in replay
        )
        accuracy = rate(correct, len(replay))
        accuracy["unavailable"] = sum(
            not evidence_complete(row) or references.get(row["clip_id"]) is None
            for row in replay
        )
        if accuracy["unavailable"]:
            accuracy["rate"] = None
        operations = [operation_matches(row, by_id[row["clip_id"]]) for row in safe]
        wrong = rate(sum(item is not True for item in operations), len(operations))
        wrong["unavailable"] = sum(item is None for item in operations)
        errors = total = baseline_errors = unavailable_wer = 0
        for row in replay:
            reference = references.get(row["clip_id"])
            if (
                reference is None
                or not evidence_complete(row)
                or not isinstance(row.get("baseline_transcript"), str)
            ):
                unavailable_wer += 1
                continue
            paired = baseline.get("recordings", {}).get(row["clip_id"], {})
            if (
                paired.get("audio_sha256") != row["audio_sha256"]
                or paired.get("transcript") != row["baseline_transcript"]
                or paired.get("reply_group") != row["reply_group"]
            ):
                raise ReplayError("PAIRED_BASELINE_TRANSCRIPT_MISMATCH")
            distance, count = word_errors(reference, row["recognized_transcript"])
            baseline_distance, _ = word_errors(reference, row["baseline_transcript"])
            errors += distance
            total += count
            baseline_errors += baseline_distance
        wer = {
            "errors": errors,
            "words": total,
            "rate": errors / total if total and not unavailable_wer else None,
            "baseline_rate": baseline_errors / total
            if total and not unavailable_wer
            else None,
            "unavailable": unavailable_wer,
        }
        latency = latency_gate(
            {
                group: data["steps"]["last_voice_to_playback"]
                for group, data in baseline["profiles"]["baseline"][
                    "reply_groups"
                ].items()
            },
            {
                profile: {
                    group: data["steps"]["last_voice_to_playback"]
                    for group, data in data["reply_groups"].items()
                }
                for profile, data in profiles.items()
            },
        )
        fault = fault_gate(faults or {})
        result.update(
            latency=latency,
            pauses=pause,
            executable_prefix_cuts=prefix_result,
            accuracy=accuracy,
            wrong_actions=wrong,
            word_error_rate=wer,
            faults=fault,
        )
        gates.update(
            latency=latency["passed"],
            pauses=pause["passed"],
            executable_prefix=len(prefix) >= 30 and prefix_result["count"] == 0,
            accuracy=accuracy["rate"] is not None and accuracy["rate"] >= 0.95,
            wrong_actions=bool(safe)
            and wrong["count"] == 0
            and wrong["unavailable"] == 0,
            word_error_rate=bool(total)
            and not unavailable_wer
            and errors <= baseline_errors + 0.01 * total,
            faults=fault["passed"],
        )
        gates["pause_coverage"] = {row["clip_id"] for row in pauses} == {
            label["id"]
            for label in labels
            if label.get("split") == "held_out" and label.get("set") == "pause"
        }
        gates["pause_samples"] = len(pauses) >= 300
    else:
        result["recordings"] = {
            row["clip_id"]: {
                "audio_sha256": row["audio_sha256"],
                "transcript": row["recognized_transcript"],
                "reply_group": row["reply_group"],
            }
            for row in primary
        }
    result["gates"] = gates
    result["accepted"] = all(gates.values())
    return result


def markdown(report: Row) -> str:
    lines = [
        f"# {report['phase'].upper()} measurement report",
        "",
        f"Acceptance: {'PASS' if report['accepted'] else 'FAIL'}.",
        "",
        "Percentiles use linear interpolation. "
        "Rate intervals use two-sided Clopper-Pearson at 95%.",
        "",
        "| Profile | Reply group | Step | Samples | Missing | "
        "p50 ms | p90 ms | Quality |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for profile, data in report["profiles"].items():
        for group, values in data["reply_groups"].items():
            for step, timing_data in values["steps"].items():
                lines.append(
                    f"| {profile} | {group} | {timing_data.get('label', step)} | "
                    f"{timing_data['n']} | "
                    f"{timing_data['missing']} | {timing_data['p50_ms']} | "
                    f"{timing_data['p90_ms']} | {timing_data['quality']} |"
                )
    lines.extend(
        [
            "",
            "The JSON report contains denominators, confidence intervals, "
            "exclusions, and event quality.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="phase", required=True)
    for phase in ("p0", "p1"):
        command = subparsers.add_parser(phase)
        for name in ("labels", "runs", "calibration", "native", "output"):
            command.add_argument("--" + name, type=Path, required=True)
        command.add_argument("--split", choices=("held_out",), required=True)
        command.add_argument("--markdown", type=Path)
        command.add_argument("--check", action="store_true")
        command.add_argument("--correct-transcripts", type=Path)
        if phase == "p1":
            command.add_argument("--baseline", type=Path, required=True)
            command.add_argument("--faults", type=Path, required=True)
            command.add_argument("--latency-profile", choices=("safe",), default="safe")
    args = parser.parse_args(argv)
    try:
        labels = load_corpus(args.labels)
        if not frozen_corpus(args.labels, args.labels.parent / "corpus.sha256", labels):
            raise ReplayError("BLOCKED corpus not frozen")
        transcript_path = (
            args.correct_transcripts or args.labels.parent / "correct_transcripts.jsonl"
        )
        correct_transcripts = None
        if args.phase == "p1" and transcript_path.exists():
            if not check_digest(transcript_path):
                raise ReplayError("CORRECT_TRANSCRIPT_DIGEST_MISMATCH")
            correct_transcripts = load_corpus(transcript_path)
        report = build_report(
            args.phase,
            load_corpus(args.runs),
            labels,
            json.loads(args.calibration.read_text()),
            json.loads(args.native.read_text()),
            baseline=json.loads(args.baseline.read_text())
            if args.phase == "p1"
            else None,
            faults=json.loads(args.faults.read_text()) if args.phase == "p1" else None,
            correct_transcripts=correct_transcripts,
        )
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        if args.markdown:
            args.markdown.write_text(markdown(report))
        names = {
            "completeness": "P0 completeness>=95%"
            if args.phase == "p0"
            else "completeness>=95%",
            "native_matrix": "native matrix",
            "latency": "P1 latency",
            "pauses": "P1 pauses",
            "accuracy": "P1 accuracy",
            "wrong_actions": "P1 wrong-actions",
            "faults": "faults 40/40",
        }
        for gate, passed in report["gates"].items():
            print(f"{'PASS' if passed else 'FAIL'} {names.get(gate, gate)}")
        print(f"{'PASS' if report['accepted'] else 'FAIL'} {args.phase.upper()}")
        return int(args.check and not report["accepted"])
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"FAIL report: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
