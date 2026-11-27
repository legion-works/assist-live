import asyncio
import copy
import errno
import hashlib
import json
import math
import os
import random
import shlex
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from tools.device_replay import (
    ACOUSTIC_METHOD,
    DEVICE_STAGES,
    AlsaCapture,
    AlsaOutput,
    Capture,
    ClientEvents,
    ReplayError,
    align_capture,
    attach_evidence,
    baseline_for,
    check_options,
    frozen_corpus,
    parser,
    pause_cut,
    replay_sample,
    run,
    sample_errors,
    validate_sample,
)

ROOT = Path(__file__).resolve().parents[1]
TURN = "01900000-0000-7000-8000-000000000001"


def event(stage, offset=0, turn_id=TURN, source="device"):
    return {
        "turn_id": turn_id,
        "satellite": "test_satellite",
        "source": source,
        "stage": stage,
        "t_utc_ns": 10_000_000_000 + offset * 1_000_000,
        "quality": "estimate",
        "uncertainty_ms": 2.0,
        "t_device_ms": offset if source == "device" else None,
        "seq": offset if source == "device" else None,
        "data": {},
    }


def sample():
    events = [event(stage, i * 1000) for i, stage in enumerate(DEVICE_STAGES)]
    internal = [
        event(stage, ms, source="integration")
        for stage, ms in (
            ("end_decision", 1500),
            ("stt_final", 1600),
            ("tts_first_byte", 1750),
        )
    ]
    return {
        "schema_version": 2,
        "mode": "acceptance",
        "clip_id": "clip-01",
        "audio_sha256": "a" * 64,
        "split": "held_out",
        "phase": "p0",
        "profile": "baseline",
        "frozen": True,
        "run_id": "test-run",
        "baseline_config_id": "config-sha256-01",
        "turn_id": TURN,
        "calibration_revision": "calibration-01",
        "recognized_transcript": "turn on test light",
        "evaluated_intent": {"name": "HassTurnOn", "slots": {"name": "virtual light"}},
        "action_trace": [
            {
                "service": "light.turn_on",
                "virtual_target": "light.virtual_light",
                "arguments": {},
                "monotonic_ns": 123,
            }
        ],
        "action_trace_complete": True,
        "reply_text": "Done.",
        "reply_char_count": 5,
        "reply_group": "short",
        "events": events + internal,
        **{stage: item for stage, item in zip(DEVICE_STAGES, events, strict=True)},
        "host_timing": {
            "mark_start_utc_ns": 1,
            "mark_start_monotonic_ns": 1,
            "command_play_requested_utc_ns": 2,
        },
        "server_clock": {
            "kind": "same_host",
            "offset_ns": 0,
            "uncertainty_ms": 0,
            "source": "local-interface",
        },
        "alignment": {
            "method": ACOUSTIC_METHOD,
            "quality": "estimate",
            "uncertainty_ms": 10,
            "clock_uncertainty_ms": 0,
            "last_voice_utc_ns": 11_500_000_000,
            "reply_start_utc_ns": 12_000_000_000,
            "reply_end_utc_ns": 13_000_000_000,
        },
    }


def pause_fit_case(decision_data, fit_complete):
    row = sample()
    row["phase"] = "p1"
    speech = event("speech_start", 100, source="server")
    speech["data"]["onset_ms"] = 100
    row["events"] = [
        speech,
        {**event("end_decision", 900, source="server"), "data": decision_data},
    ]
    row["alignment"].update(
        first_voice_utc_ns=10_000_000_000,
        last_voice_utc_ns=10_750_000_000,
        last_voice_sample=13400,
        uncertainty_ms=100.01,
        clock_uncertainty_ms=100,
        acoustic_uncertainty_ms=0.01,
        command_offset_samples=1400,
    )
    if fit_complete is not None:
        row["pause_fit"] = {
            "offset_samples": -200,
            "reference_samples": 16000,
            "uncertainty_samples": 2,
            "complete": fit_complete,
        }
    label = {
        "last_voice_sample": 12000,
        "sample_rate": 16000,
        "pause_intervals_ms": [[100, 400]],
    }
    return row, label


@pytest.mark.parametrize(
    ("decision_data", "fit_complete", "cut", "rule", "reason"),
    [
        ({"end_sample": 7999}, True, True, "position", None),
        ({"end_sample": 8002}, True, True, "position", None),
        ({"end_sample": 8003}, True, False, "position", None),
        ({"end_sample": 9000}, True, False, "position", None),
        ({"end_sample": 7999}, False, False, "clock", "fit_failed"),
        ({"end_sample": 7999}, None, False, "clock", "no_debug_recording"),
        ({"end_sample": 16001}, True, False, "clock", "fit_failed"),
        ({}, True, False, "clock", "no_end_sample"),
        ({"end_sample": True}, True, False, "clock", "no_end_sample"),
        ({"end_sample": -1}, True, False, "clock", "no_end_sample"),
    ],
    ids=[
        "cut",
        "uncertain",
        "boundary",
        "after",
        "bad-fit",
        "no-wav",
        "outside-wav",
        "old",
        "bool",
        "negative",
    ],
)
def test_pause_rule_uses_fitted_audio_position(
    decision_data, fit_complete, cut, rule, reason
):
    row, label = pause_fit_case(decision_data, fit_complete)
    assert pause_cut(row, label) is cut
    assert row["pause_rule"] == rule
    assert row.get("pause_rule_reason") == reason


@pytest.mark.parametrize(
    ("fit_shift", "onset_ms", "consistent"),
    [
        (802, 100, True),
        (803, 100, False),
        (-803, 100, False),
        (0, None, False),
        (0, True, False),
    ],
    ids=["at-limit", "late", "early", "missing-onset", "invalid-onset"],
)
def test_pause_position_requires_consistent_first_voice(
    fit_shift, onset_ms, consistent
):
    row, label = pause_fit_case({"end_sample": 6500}, True)
    row["pause_fit"]["offset_samples"] += fit_shift
    row["events"][0]["data"] = {} if onset_ms is None else {"onset_ms": onset_ms}
    assert pause_cut(row, label) is consistent
    assert row["pause_rule"] == ("position" if consistent else "clock")
    assert row.get("pause_rule_reason") == (None if consistent else "fit_inconsistent")


@pytest.mark.parametrize(
    ("fit_complete", "reason"),
    [(True, "fit_inconsistent"), (None, "no_debug_recording")],
)
def test_pause_rule_is_not_written_when_clock_cannot_decide(fit_complete, reason):
    row, label = pause_fit_case({"end_sample": 6500}, fit_complete)
    del row["alignment"]["first_voice_utc_ns"]
    with pytest.raises(ReplayError, match="^MISSING_PAUSE_DELAY_ESTIMATE$"):
        pause_cut(row, label)
    assert "pause_rule" not in row
    assert row["pause_rule_reason"] == reason


@pytest.mark.parametrize("bad_start", ["duplicate", "negative-delay", "after-decision"])
def test_pause_clock_refuses_invalid_server_start(bad_start):
    row, label = pause_fit_case({"end_sample": 6500}, None)
    if bad_start == "duplicate":
        row["events"].append(event("speech_start", 110, source="server"))
    elif bad_start == "negative-delay":
        row["events"][0]["t_utc_ns"] = row["alignment"]["first_voice_utc_ns"] - 1
    else:
        row["events"][0]["t_utc_ns"] = row["events"][1]["t_utc_ns"] + 1

    with pytest.raises(ReplayError, match="^MISSING_PAUSE_DELAY_ESTIMATE$"):
        pause_cut(row, label)
    assert "pause_rule" not in row


def test_pause_fit_accepts_stream_start_before_capture():
    from tools.turn_audio import correlate

    rng = random.Random(128)
    command = struct.pack(
        "<12000h", *(rng.randrange(-8000, 8000) for _ in range(12000))
    )
    stream = bytes(800) + command
    external = command + bytes(3200)
    assert correlate(external, stream)["complete"] is False
    fit = correlate(external, stream, allow_early_start=True)
    assert fit["complete"] is True
    assert fit["offset_samples"] == -400


def test_websocket_only_run_is_not_p0():
    from tools.device_replay import validate_sample

    assert not validate_sample({"wake_detected": None, "playback_end": None})


def test_schema_accepts_complete_sample_and_explicit_empty_log():
    row = sample()
    assert sample_errors(row) == []
    row["action_trace"] = []
    assert validate_sample(row)
    del row["action_trace_complete"]
    assert "INCOMPLETE_OPERATION_LOG" in sample_errors(row)


def test_history_on_another_stage_is_invalid_evidence():
    row = sample()
    row["wake_detected"]["data"]["pre_roll_ms"] = 300
    assert "INVALID_PRE_ROLL_MS" in sample_errors(row)


@pytest.mark.parametrize("field", list(sample()))
def test_schema_refuses_missing_fields(field):
    row = sample()
    del row[field]
    assert not validate_sample(row), field


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("recognized_transcript", "", "MISSING_RECOGNIZED_TRANSCRIPT"),
        ("evaluated_intent", {}, "MISSING_INTENT"),
        ("action_trace", None, "MISSING_OPERATION_LOG"),
        ("action_trace_complete", False, "INCOMPLETE_OPERATION_LOG"),
        ("action_trace", [{}], "INVALID_OPERATION"),
        ("reply_char_count", 99, "REPLY_CHAR_COUNT"),
        ("reply_group", "long", "REPLY_GROUP"),
        ("profile", "fast", "PROFILE"),
        ("alignment", {"uncertainty_ms": 1}, "MISSING_ALIGNMENT"),
    ],
)
def test_schema_refuses_invalid_evidence(field, value, error):
    row = sample()
    row[field] = value
    assert error in sample_errors(row)


@pytest.mark.parametrize("stage", DEVICE_STAGES)
def test_device_boundary_cannot_be_replaced_with_websocket_event(stage):
    row = sample()
    row["events"] = [
        event(stage, source="server") if item["stage"] == stage else item
        for item in row["events"]
    ]
    assert "MISSING_DEVICE_" + stage.upper() in sample_errors(row)


def test_duplicate_wake_sample_is_refused():
    row = sample()
    row["events"].append(event("wake_detected", 10))
    assert "DUPLICATE_WAKE" in sample_errors(row)


def test_frozen_false_only_valid_for_smoke():
    row = sample()
    row["frozen"] = False
    assert not validate_sample(row)
    assert validate_sample(row, acceptance=False)


def test_even_frozen_smoke_is_not_acceptance():
    row = sample()
    row["mode"] = "smoke"
    assert not validate_sample(row)
    assert validate_sample(row, acceptance=False)


def options(*extra):
    return parser().parse_args(
        [
            "--phase",
            "p0",
            "--labels",
            "labels.jsonl",
            "--audio-root",
            ".",
            "--split",
            "held_out",
            "--wake-word",
            "--external-mic",
            "--output",
            "runs.jsonl",
            *extra,
        ]
    )


def test_acceptance_refuses_limit():
    with pytest.raises(ReplayError, match="LIMIT_NOT_ALLOWED_IN_ACCEPTANCE"):
        check_options(options("--acceptance", "--limit", "2"))
    assert check_options(options("--limit", "2")) == "baseline"


def test_documented_acceptance_commands_supply_ha_counter_sources():
    procedures = (ROOT / "docs/testing/device-replay.md").read_text().splitlines()
    commands = [
        parser().parse_args(shlex.split(line)[4:])
        for line in procedures
        if line.startswith("uv run python tools/device_replay.py ")
        and "--acceptance" in line
    ]
    assert {args.phase for args in commands} == {"p0", "p1"}
    assert {args.profile for args in commands if args.phase == "p1"} == {"safe", "fast"}
    for args in commands:
        assert all(
            (
                args.ha_url,
                args.ha_token_file,
                args.integration_entry,
                args.device_loss_entity,
            )
        ), f"ACCEPTANCE_COUNTER_SOURCES_{args.phase.upper()}"


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", [False, True])
async def test_replay_refuses_repository_output_before_corpus_load(
    alias, tmp_path, monkeypatch
):
    args = options()
    args.output = ROOT / "forbidden-evidence.jsonl"
    if alias:
        link = tmp_path / "link"
        link.symlink_to(ROOT, target_is_directory=True)
        args.output = link / args.output.name

    def no_corpus(*args):
        pytest.fail("REPLAY_OUTPUT_CHECK_PRECEDES_CORPUS_LOAD")

    monkeypatch.setattr("tools.device_replay.load_corpus", no_corpus)
    with pytest.raises(ValueError, match="outside the repository"):
        await run(args)


@pytest.mark.parametrize(
    "extra,error",
    [
        (["--profile", "safe"], "PROFILE_P1_ONLY"),
        (["--phase", "p1"], "P1_REQUIRES_PROFILE"),
        (["--limit", "0"], "INVALID_SMOKE_LIMIT"),
    ],
)
def test_option_refusals(extra, error):
    with pytest.raises(ReplayError, match=error):
        check_options(options(*extra))


def test_cli_limit_refusal(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/device_replay.py"),
            "--phase",
            "p0",
            "--labels",
            str(tmp_path / "missing"),
            "--audio-root",
            str(tmp_path),
            "--split",
            "held_out",
            "--wake-word",
            "--external-mic",
            "--acceptance",
            "--limit",
            "2",
            "--output",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "LIMIT_NOT_ALLOWED_IN_ACCEPTANCE" in result.stderr
    assert not (tmp_path / "out").exists()


def tone(length, amplitude, frequency=237):
    return [
        amplitude * math.sin(2 * math.pi * frequency * i / 16000) for i in range(length)
    ]


def signals():
    reference = (
        [0.0] * 1600
        + tone(3200, 0.4)
        + tone(1600, 0.15, 311)
        + tone(1600, 0.02, 173)
        + [0.0] * 1600
    )
    noise = [(0.0001 if i % 2 else -0.0001) for i in range(1600)]
    reply = tone(3200, 0.3, 419) + tone(1600, 0.01, 419)
    mic = noise + reference + [0.0] * 3200 + reply + [0.0] * 3200
    mic[-100] = 1.0
    return reference, mic


def test_alignment_finds_quiet_tail_and_ignores_click_with_noise():
    reference, mic = signals()
    alignment = align_capture(Capture(mic, 16000, 1_000_000_000, 3), reference, 7999)
    assert alignment["command_offset_samples"] == 1600
    assert alignment["last_voice_sample"] == 9599
    assert alignment["reply_start_sample"] == 14400
    assert alignment["reply_end_sample"] == 19199
    assert alignment["reply_start_utc_ns"] == 1_900_000_000
    assert alignment["uncertainty_ms"] >= 13
    assert alignment["quality"] == "estimate"


def test_alignment_finds_noise_reply_boundaries():
    reference, mic = signals()
    generator = random.Random(17)
    mic[14400:19200] = [generator.uniform(-0.3, 0.3) for _ in range(4800)]
    alignment = align_capture(Capture(mic, 16000, 0, 4), reference, 7999)
    assert alignment["reply_start_sample"] == 14400
    assert alignment["reply_end_sample"] == 19199


def test_alignment_refuses_truncated_reply():
    reference, mic = signals()
    with pytest.raises(ReplayError, match="ALIGNMENT_REPLY_TRUNCATED"):
        align_capture(Capture(mic[:18500], 16000, 0, 4), reference, 7999)


@pytest.mark.parametrize(
    "case,error",
    [
        ("silence", "ALIGNMENT_COMMAND_MISSING"),
        ("reply_missing", "ALIGNMENT_REPLY_MISSING"),
        ("wrong_boundary", "ALIGNMENT_BOUNDARY"),
        ("bad_rate", "ALIGNMENT_INPUT"),
    ],
)
def test_alignment_refusal(case, error):
    reference, mic = signals()
    rate, boundary = 16000, 7999
    if case == "silence":
        mic = [0.0] * len(mic)
    elif case == "reply_missing":
        mic = [0.0] * 1600 + reference + [0.0] * 6400
    elif case == "wrong_boundary":
        boundary = 3000
    else:
        rate = 8000
    with pytest.raises(ReplayError, match=error):
        align_capture(Capture(mic, rate, 0, 1), reference, boundary)


def bundle(tmp_path, *, pause=False):
    if pause:
        quiet = [0.0] * 6400
        reference = (
            [0.0] * 800
            + tone(1600, 0.4)
            + quiet
            + tone(1600, 0.15)
            + quiet
            + tone(1600, 0.25)
        )
        mic = [0.0] * 1600 + reference + [0.0] * 3200 + tone(3200, 0.3) + quiet
    else:
        reference, mic = signals()
    path = tmp_path / ("pause.wav" if pause else "command.wav")
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(
            b"".join(struct.pack("<h", round(value * 32767)) for value in reference)
        )
    label = {
        "id": "clip-01",
        "set": "pause" if pause else "replay",
        "split": "held_out",
        "audio_path": path.name,
        "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sample_rate": 16000,
        "last_voice_sample": len(reference) - 1 if pause else 7999,
        "intent": {"name": "HassTurnOn", "slots": {"name": "virtual light"}},
    }
    if pause:
        label["pause_intervals_ms"] = [[150, 550], [650, 1050]]
    return label, Capture(mic, 16000, 11_100_000_000 if pause else 10_000_000_000, 4)


class FakeOutput:
    def __init__(self):
        self.played = []

    async def play(self, path):
        self.played.append(path.name)


class FakeCapture:
    def __init__(self, recording):
        self.recording = recording
        self.started = self.finished = self.aborted = False

    async def start(self):
        self.started = True

    async def finish(self):
        self.finished = True
        return self.recording

    async def abort(self):
        self.aborted = True


class FakeEvents:
    def __init__(self, events=None, exported=None):
        self.server_clock = {
            "kind": "same_host",
            "offset_ns": 0,
            "uncertainty_ms": 0,
            "source": "local-interface",
        }
        self.events = (
            [event(stage, i * 1000) for i, stage in enumerate(DEVICE_STAGES)]
            if events is None
            else events
        )
        self.exported = self.events if exported is None else exported
        self.queue = iter(self.events)
        self.closed = False

    async def start(self, since):
        self.since = since

    async def next(self):
        try:
            return next(self.queue)
        except StopIteration:
            raise ReplayError("MISSING_DEVICE_EVENT") from None

    async def turn(self, turn_id):
        return {"turn_id": turn_id, "turn_type": "voice"}

    async def export(self, since):
        return self.exported

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_replay_transcript_does_not_use_last_final_by_position(tmp_path):
    label, recording = bundle(tmp_path)
    events = FakeEvents().events
    for source, transcript in (("server", "owned text"), ("integration", "wrong text")):
        final = event("stt_final", 1500, source=source)
        final["data"]["transcript"] = transcript
        events.append(final)
    row = await replay_sample(
        label,
        tmp_path,
        tmp_path / "wake.wav",
        FakeOutput(),
        FakeCapture(recording),
        FakeEvents(exported=events),
    )
    assert row["recognized_transcript"] == "owned text", "OWNER_NOT_POSITION"


@pytest.mark.asyncio
async def test_runner_plays_wake_then_command_and_exports_only_timing(tmp_path):
    label, recording = bundle(tmp_path)
    output, capture, source = FakeOutput(), FakeCapture(recording), FakeEvents()
    row = await replay_sample(
        label, tmp_path, tmp_path / "wake.wav", output, capture, source
    )
    assert output.played == ["wake.wav", "command.wav"]
    assert capture.started and capture.finished and capture.aborted and source.closed
    assert row["alignment"]["reply_start_utc_ns"] == 10_900_000_000
    assert (
        row["alignment"]["reply_start_utc_ns"]
        != row["host_timing"]["command_play_requested_utc_ns"]
    )
    assert recording.samples == []
    assert '"samples":' not in json.dumps(row)


@pytest.mark.asyncio
async def test_hash_mismatch_stops_before_any_playback(tmp_path):
    label, recording = bundle(tmp_path)
    label["audio_sha256"] = "0" * 64
    output = FakeOutput()
    with pytest.raises(ReplayError, match="AUDIO_HASH_OR_FORMAT_MISMATCH"):
        await replay_sample(
            label,
            tmp_path,
            tmp_path / "wake.wav",
            output,
            FakeCapture(recording),
            FakeEvents(),
        )
    assert output.played == []


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [None, 0, 300, 1000])
async def test_runner_keeps_history_and_waits_for_microphone_open(tmp_path, history):
    label, recording = bundle(tmp_path)
    source = FakeEvents()
    if history is not None:
        source.events[1]["data"]["pre_roll_ms"] = history

    class ReadyOutput(FakeOutput):
        async def play(self, path):
            if path.name == "command.wav":
                assert next(source.queue)["stage"] == "playback_start"
                source.queue = iter(source.events[2:])
            await super().play(path)

    row = await replay_sample(
        label,
        tmp_path,
        tmp_path / "wake.wav",
        ReadyOutput(),
        FakeCapture(recording),
        source,
    )
    assert row["mic_open"] == source.events[1]
    assert row["mic_open"]["data"] == (
        {} if history is None else {"pre_roll_ms": history}
    )
    assert row["alignment"]["reply_start_utc_ns"] == 10_900_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [-1, 1001, True, 1.5, "300", None])
async def test_runner_refuses_invalid_history_before_command(tmp_path, history):
    label, recording = bundle(tmp_path)
    source = FakeEvents()
    source.events[1]["data"]["pre_roll_ms"] = history
    output, capture = FakeOutput(), FakeCapture(recording)
    with pytest.raises(ReplayError, match="INVALID_PRE_ROLL_MS"):
        await replay_sample(
            label, tmp_path, tmp_path / "wake.wav", output, capture, source
        )
    assert output.played == ["wake.wav"]
    assert not capture.started
    assert capture.aborted and source.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,error",
    [
        ("duplicate", "DUPLICATE_WAKE"),
        ("unmatched", "UNMATCHED_TURN"),
        ("missing_end", "MISSING_DEVICE_EVENT"),
        ("no_wake", "MISSING_DEVICE_WAKE_DETECTED"),
        ("late_duplicate", "DUPLICATE_WAKE"),
    ],
)
async def test_runner_refuses_device_failures(tmp_path, case, error):
    label, recording = bundle(tmp_path)
    source = FakeEvents()
    items = copy.deepcopy(source.events)
    if case == "duplicate":
        items.insert(1, event("wake_detected", 1))
    elif case == "unmatched":
        items[1]["turn_id"] = None
    elif case == "missing_end":
        items.pop()
    elif case == "no_wake":
        items.pop(0)
    if case == "late_duplicate":
        source = FakeEvents(exported=items + [event("wake_detected", 4000)])
    else:
        source = FakeEvents(items)
    capture = FakeCapture(recording)
    with pytest.raises(ReplayError, match=error):
        await replay_sample(
            label, tmp_path, tmp_path / "wake.wav", FakeOutput(), capture, source
        )
    assert capture.aborted and source.closed


def recorder(row):
    call = {
        "domain": "light",
        "service": "turn_on",
        "context_id": "test-context",
        "service_data": {"entity_id": "light.virtual_light"},
    }
    return {
        "turn_id": row["turn_id"],
        "turn_type": "voice",
        "device_id": "virtual-device",
        "run_id": row["run_id"],
        "baseline_configuration_id": row["baseline_config_id"],
        "recognized_transcript": row["recognized_transcript"],
        "selected_intent": row["evaluated_intent"],
        "context_id": "test-context",
        "response_type": "action_done",
        "actions": [{"kind": "service", **call}],
        "service_calls": [call],
        "reply": row["reply_text"],
        "text_deltas": [],
        "group": "short",
        "operations": [
            {
                "service": action["service"],
                "target": action["virtual_target"],
                "arguments": action["arguments"],
                "t_monotonic_ns": action["monotonic_ns"],
            }
            for action in row["action_trace"]
        ],
        "completed": True,
    }


def test_recorder_bridge_reuses_virtual_recorder_and_refuses_other_runs():
    row = sample()
    record = recorder(row)
    label = {"intent": row["evaluated_intent"]}
    assert attach_evidence(row, label, [record])["action_trace_complete"] is True
    for key in ("run_id", "turn_id", "baseline_configuration_id"):
        invalid = {**record, key: "other"}
        with pytest.raises(ReplayError, match="MISSING_OPERATION_EVIDENCE"):
            attach_evidence(row, label, [invalid])
    with pytest.raises(ReplayError, match="INVALID_OPERATION_EVIDENCE"):
        attach_evidence(row, label, [{**record, "actions": []}])
    with pytest.raises(ReplayError, match="TRANSCRIPT_EVIDENCE_MISMATCH"):
        attach_evidence(row, label, [{**record, "recognized_transcript": "different"}])


@pytest.mark.parametrize(
    "field",
    ["turn_id", "run_id", "baseline_configuration_id", "completed", "operations"],
)
def test_bridge_missing_field_is_named(field):
    row = sample()
    record = recorder(row)
    del record[field]
    with pytest.raises(ReplayError, match="MISSING_BRIDGE_FIELD: " + field):
        attach_evidence(row, {"intent": row["evaluated_intent"]}, [record])


def test_text_only_recorder_shape_is_refused_as_named_gap():
    row = sample()
    record = recorder(row)
    for field in (
        "turn_id",
        "run_id",
        "baseline_configuration_id",
        "completed",
        "operations",
    ):
        del record[field]
    with pytest.raises(ReplayError, match="MISSING_BRIDGE_FIELD: turn_id"):
        attach_evidence(row, {"intent": row["evaluated_intent"]}, [record])


@pytest.mark.parametrize("field", ["service", "target", "arguments", "t_monotonic_ns"])
def test_bridge_operation_missing_field_is_named(field):
    row = sample()
    record = recorder(row)
    del record["operations"][0][field]
    with pytest.raises(ReplayError, match="MISSING_BRIDGE_OPERATION_FIELD: " + field):
        attach_evidence(row, {"intent": row["evaluated_intent"]}, [record])


def test_bridge_incomplete_recorder_is_refused():
    row = sample()
    record = {**recorder(row), "completed": False}
    with pytest.raises(ReplayError, match="INCOMPLETE_OPERATION_LOG"):
        attach_evidence(row, {"intent": row["evaluated_intent"]}, [record])


def test_p1_pairs_same_recording_hash_split_and_configuration():
    row = sample()
    label = {
        "id": row["clip_id"],
        "audio_sha256": row["audio_sha256"],
        "split": row["split"],
    }
    assert (
        baseline_for(label, row["baseline_config_id"], [row])
        == row["recognized_transcript"]
    )
    for key in ("audio_sha256", "baseline_config_id", "split", "clip_id"):
        with pytest.raises(ReplayError, match="MISSING_PAIRED_BASELINE"):
            baseline_for(label, row["baseline_config_id"], [{**row, key: "other"}])
    p1 = {**row, "phase": "p1", "profile": "safe"}
    assert "MISSING_BASELINE_TRANSCRIPT" in sample_errors(p1)


def test_frozen_manifest_checks_labels_and_audio(tmp_path):
    labels, manifest = tmp_path / "labels.jsonl", tmp_path / "corpus.sha256"
    labels.write_text("{}\n")
    rows = [{"audio_path": "clip.wav", "audio_sha256": "a" * 64}]
    manifest.write_text(
        hashlib.sha256(labels.read_bytes()).hexdigest()
        + "  labels\n"
        + "a" * 64
        + "  audio/clip.wav\n"
    )
    assert frozen_corpus(labels, manifest, rows)
    labels.write_text("{}\n{}\n")
    assert not frozen_corpus(labels, manifest, rows)


@pytest.mark.asyncio
async def test_acceptance_requires_frozen_corpus_smoke_does_not(tmp_path, capsys):
    label, _ = bundle(tmp_path)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps(label) + "\n")
    args = options()
    args.labels, args.output = labels, tmp_path / "runs.jsonl"
    with pytest.raises(ReplayError, match="BLOCKED corpus not frozen"):
        await run(args)
    args.limit = 2
    with pytest.raises(ReplayError, match="REPLAY_SETUP_NOT_AVAILABLE"):
        await run(args)
    assert "NOT FROZEN corpus" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_pauses", "before", "after", "error"),
    [
        (False, "safe", "safe", None),
        (True, "safe", "safe", None),
        (False, None, "safe", "MISSING_TURN_PROFILE"),
        (False, "fast", "safe", "TURN_PROFILE_MISMATCH"),
        (False, "http-error", "safe", "TURN_PROFILE_HEALTH_UNAVAILABLE"),
        (False, "safe", None, "MISSING_TURN_PROFILE"),
        (True, "safe", "fast", "TURN_PROFILE_MISMATCH"),
        (False, "safe", "invalid-json", "TURN_PROFILE_HEALTH_UNAVAILABLE"),
    ],
    ids=[
        "smoke",
        "pause-report",
        "missing-before",
        "wrong-before",
        "http-error-before",
        "missing-after",
        "wrong-after",
        "invalid-json-after",
    ],
)
async def test_p1_replay_verifies_profile_before_and_after_trials(
    tmp_path, monkeypatch, include_pauses, before, after, error
):
    from aiohttp import ClientResponseError, ClientSession

    from tools.report import build_report, markdown

    label, replay_capture = bundle(tmp_path)
    corpus = [label]
    captures = [replay_capture]
    decisions = [11_450_000_000]
    if include_pauses:
        pause, pause_capture = bundle(tmp_path, pause=True)
        for name, elapsed_ms in (
            ("cut", 1130),
            ("uncertain", 1180),
            ("boundary", 1181),
            ("after", 1450),
            ("missing", None),
            ("no-delay", 1450),
            ("position", 1450),
            ("no-wav", 1450),
            ("fit-failed", 1450),
        ):
            corpus.append(
                {
                    **pause,
                    "id": name,
                    "executable_prefix": True,
                    "executable_prefix_intervals_ms": [[50, 150]],
                }
            )
            captures.append(pause_capture)
            decisions.append(
                11_450_000_000 + elapsed_ms * 1_000_000 if elapsed_ms else None
            )
        corpus.append({**pause, "id": "tuning-pause", "split": "tuning"})
    labels = tmp_path / "labels.jsonl"
    labels.write_text("".join(json.dumps(item) + "\n" for item in corpus))
    if include_pauses:
        manifest = {item["audio_path"]: item["audio_sha256"] for item in corpus}
        (tmp_path / "corpus.sha256").write_text(
            hashlib.sha256(labels.read_bytes()).hexdigest()
            + "  labels\n"
            + "".join(f"{digest}  audio/{path}\n" for path, digest in manifest.items())
        )
    records_path = tmp_path / "operations.jsonl"
    baseline = sample()
    baseline.update(clip_id=label["id"], audio_sha256=label["audio_sha256"])
    baselines = tmp_path / "baseline.jsonl"
    baselines.write_text(json.dumps(baseline) + "\n")
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token": "test-only"}))
    clock_record = tmp_path / "clock.json"
    clock_record.write_text(json.dumps(measured_clock(250_000_000, 7)))
    for key, value in {
        "ASSIST_LIVE_URL": "http://127.0.0.1",
        "ASSIST_LIVE_TOKEN_FILE": str(token),
        "ASSIST_LIVE_SATELLITE": "test_satellite",
        "ASSIST_LIVE_WAKE_AUDIO": "wake.wav",
        "ASSIST_LIVE_RUN_ID": "test-run",
        "ASSIST_LIVE_BASELINE_CONFIG_ID": baseline["baseline_config_id"],
        "ASSIST_LIVE_CALIBRATION_REVISION": "calibration-01",
        "ASSIST_LIVE_OPERATION_RECORDS": str(records_path),
        "ASSIST_LIVE_BASELINE_RUNS": str(baselines),
    }.items():
        monkeypatch.setenv(key, value)

    class FakeClient:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    records = []
    block_start = 0
    listings = {}
    copies = []
    debug_source = "/config/pipeline_recordings/device/Pipeline/new/01_stt-test.wav"

    async def debug_docker(action, *args):
        assert args[0].split(":", 1)[0] == "test-ha"
        if action == "cp":
            assert args[0] == f"test-ha:{debug_source}"
            target = Path(args[1])
            assert target.stat().st_mode & 0o777 == 0o600
            assert target.parent.stat().st_mode & 0o777 == 0o700
            target.write_bytes((tmp_path / "pause.wav").read_bytes())
            copies.append(target)
            return ""
        assert action == "exec"
        if args[-1] == debug_source:
            return hashlib.sha256((tmp_path / "pause.wav").read_bytes()).hexdigest()
        assert args[-1] == "/config/pipeline_recordings/device/Pipeline"
        count = listings[len(records)] = listings.get(len(records), 0) + 1
        clip_id = corpus[(len(records) - 1) % len(decisions)]["id"]
        sources = [debug_source] if count == 2 and clip_id != "no-wav" else []
        return json.dumps(sources)

    def fit(external, debug, *, allow_early_start):
        assert allow_early_start is True
        assert len(external) == len(pause_capture.samples) * 2
        assert debug and any(debug)
        clip_id = corpus[(len(records) - 1) % len(decisions)]["id"]
        return {
            "complete": clip_id == "position",
            "offset_samples": -200,
            "uncertainty_samples": 4,
            "reference_samples": 16000,
        }

    monkeypatch.setattr("tools.device_replay.docker_command", debug_docker)
    monkeypatch.setattr("tools.device_replay.correlate", fit)

    async def capture_loss_without_network(*args, **kwargs):
        from tools.measurement import capture_loss

        return await capture_loss(None, satellites=kwargs["satellites"])

    monkeypatch.setattr(
        "tools.device_replay.capture_loss", capture_loss_without_network
    )

    class RecordingEvents(FakeEvents):
        def __init__(self, client, satellite, clock):
            turn = f"01900000-0000-7000-8000-{len(records) + 10:012x}"
            index = len(records) % len(decisions)
            stamp = decisions[index]
            items = [
                event(stage, ms, turn)
                for stage, ms in zip(DEVICE_STAGES, (0, 1000, 3500, 4000), strict=True)
            ]
            owner = "integration" if args.phase == "p0" else "server"
            transcript = event("stt_final", 3000, turn, owner)
            transcript["data"] = {"transcript": baseline["recognized_transcript"]}
            internal = [transcript, event("tts_first_byte", 3250, turn, owner)]
            clip_id = corpus[index]["id"]
            if corpus[index]["set"] == "pause" and clip_id != "no-delay":
                detected_ms = 1560 if clip_id == "boundary" else 1600
                speech = event("speech_start", detected_ms, turn, "server")
                speech["data"]["onset_ms"] = 163
                internal.insert(0, speech)
            if stamp is not None:
                internal.append(
                    {**event("end_decision", 0, turn, owner), "t_utc_ns": stamp}
                )
                if clip_id in ("position", "no-wav", "fit-failed"):
                    internal[-1]["data"]["end_sample"] = 14000
            super().__init__(
                items,
                items + internal,
            )
            self.server_clock = clock
            row = copy.deepcopy(baseline)
            row["turn_id"] = turn
            row["run_id"] = os.environ["ASSIST_LIVE_RUN_ID"]
            records.append(recorder(row))

        async def close(self):
            await super().close()
            records_path.write_text(
                "".join(json.dumps(record) + "\n" for record in records[block_start:])
            )

    class HealthResponse:
        def __init__(self, profile):
            self.profile = profile

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            if self.profile == "http-error":
                raise ClientResponseError(None, (), status=503)

        async def json(self):
            if self.profile == "invalid-json":
                raise ValueError("invalid JSON")
            if self.profile == "http-error":
                return {"turn_profile": "safe"}
            return {} if self.profile is None else {"turn_profile": self.profile}

    health_calls = []
    replies = iter((before, after, "fast", "fast"))

    def health(self, url, *, headers):
        assert url == "http://127.0.0.1/health"
        assert headers == {"Authorization": "Bearer test-only"}
        health_calls.append(len(records))
        return HealthResponse(next(replies))

    monkeypatch.setattr(ClientSession, "get", health)

    monkeypatch.setattr("tools.device_replay.AssistLiveClient", FakeClient)
    monkeypatch.setattr("tools.device_replay.AlsaOutput", lambda *args: FakeOutput())
    monkeypatch.setattr(
        "tools.device_replay.AlsaCapture",
        lambda *args: FakeCapture(
            copy.deepcopy(captures[len(records) % len(captures)])
        ),
    )
    monkeypatch.setattr("tools.device_replay.ClientEvents", RecordingEvents)
    args = options("--phase", "p1", "--profile", "safe", "--limit", "1")
    if include_pauses:
        args.limit = None
        args.ha_container = "test-ha"
        args.ha_device_id = "device"
        args.ha_pipeline_name = "Pipeline"
    args.labels, args.audio_root = labels, tmp_path
    args.output = tmp_path / "runs.jsonl"
    args.clock_record = clock_record
    if error and before != "safe":
        with pytest.raises(ReplayError, match=f"^{error}$"):
            await run(args)
        assert not records and not args.output.exists()
        assert health_calls == [0]
        return

    if error:
        with pytest.raises(ReplayError, match=f"^{error}$"):
            await run(args)
        expected = [(item["id"], item["set"]) for item in corpus[: len(decisions)]]
        assert health_calls == [0, len(expected)]
        rejected = [json.loads(line) for line in args.output.read_text().splitlines()]
        assert [(row["clip_id"], row["set"]) for row in rejected] == expected
        for row in rejected:
            assert row["complete"] is False
            assert error in row["failures"]
            assert "pause_cut" not in row
            assert "pause_rule" not in row
        return

    safe_rows = await run(args)
    safe_output = args.output
    with pytest.raises(ReplayError, match="STALE_OPERATION_EVIDENCE"):
        await run(args)
    records_path.unlink()
    block_start = len(records)
    args.profile = "fast"
    args.output = tmp_path / "fast.jsonl"
    monkeypatch.setenv("ASSIST_LIVE_RUN_ID", "fast-run")
    fast_rows = await run(args)
    rows = safe_rows + fast_rows
    assert health_calls == [0, len(safe_rows), len(safe_rows), len(rows)]
    assert all(row["loss_counters"]["start"]["observed_utc_ns"] > 0 for row in rows)
    assert all(row["loss_counters"]["end"]["observed_utc_ns"] > 0 for row in rows)
    assert all(row["loss_unavailable"] and row["reset_unknown"] for row in rows)
    if include_pauses:
        written = [
            json.loads(line)
            for path in (safe_output, args.output)
            for line in path.read_text().splitlines()
        ]
        assert written == rows
        for profile in ("safe", "fast"):
            trials = {row["clip_id"]: row for row in rows if row["profile"] == profile}
            assert trials.keys() == {item["id"] for item in corpus[:-1]}
            assert trials["cut"]["pause_cut"] is True
            assert trials["uncertain"]["pause_cut"] is True
            assert trials["boundary"]["pause_cut"] is False
            assert trials["after"]["pause_cut"] is False
            assert trials["after"]["pause_rule_reason"] == "no_end_sample"
            assert trials["position"]["pause_cut"] is True
            assert trials["position"]["pause_rule"] == "position"
            for name, reason in (
                ("no-wav", "no_debug_recording"),
                ("fit-failed", "fit_failed"),
            ):
                assert trials[name]["pause_cut"] is False
                assert trials[name]["pause_rule"] == "clock"
                assert trials[name]["pause_rule_reason"] == reason
            assert all(
                trials[key]["complete"]
                for key in ("cut", "uncertain", "boundary", "after")
            )
            assert "pause_cut" not in trials["missing"]
            assert "pause_rule" not in trials["missing"]
            assert trials["missing"]["complete"] is False
            assert "MISSING_PAUSE_MAPPING" in trials["missing"]["failures"]
            assert "pause_cut" not in trials["no-delay"]
            assert "pause_rule" not in trials["no-delay"]
            assert trials["no-delay"]["complete"] is False
            assert "MISSING_PAUSE_DELAY_ESTIMATE" in trials["no-delay"]["failures"]
            assert "pause_cut" not in trials[label["id"]]
        assert len(copies) == 4
        assert all(not path.parent.exists() for path in copies)
        report = build_report(
            "p1",
            written,
            corpus,
            {},
            [],
            baseline={
                "schema_version": 1,
                "phase": "p0",
                "accepted": True,
                "baseline_config_id": baseline["baseline_config_id"],
                "calibration_revision": baseline["calibration_revision"],
                "profiles": {"baseline": {"reply_groups": {}}},
            },
        )
        assert report["gates"]["pause_coverage"] is True
        assert report["pauses"]["denominator"] == 9
        assert report["executable_prefix_cuts"]["denominator"] == 9
        assert (
            report["pauses"]["count"] == report["executable_prefix_cuts"]["count"] == 5
        )
        assert report["pause_rules"] == {
            profile: {"position": 1, "clock": 6} for profile in ("safe", "fast")
        }
        rendered = markdown(report)
        assert (
            "| safe | position | 1 |" in rendered and "| fast | clock | 6 |" in rendered
        )
        assert report["gates"]["pauses"] is False
    else:
        assert [row["profile"] for row in rows] == ["safe", "fast"]
        assert all(row["set"] == "replay" and row["mode"] == "smoke" for row in rows)
        assert all(
            row["baseline_transcript"] == baseline["recognized_transcript"]
            and validate_sample(row, acceptance=False)
            and not row["frozen"]
            for row in rows
        )
    records_path.unlink()
    records.clear()
    block_start = 0
    health_calls.clear()
    args.phase, args.profile = "p0", None
    args.ha_container = args.ha_device_id = args.ha_pipeline_name = None
    rows = await run(args)
    assert all(row["set"] == "replay" for row in rows)
    assert [row["profile"] for row in rows] == ["baseline"]
    assert health_calls == []


@pytest.mark.asyncio
async def test_audio_subprocess_nonzero_is_named_without_hardware(
    monkeypatch, tmp_path
):
    class Process:
        returncode = 2

        class Output:
            async def read(self, count):
                return b""

        stdout = Output()

        async def wait(self):
            return 2

    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr("tools.device_replay.shutil.which", lambda name: name)
    monkeypatch.setattr("tools.device_replay.asyncio.create_subprocess_exec", spawn)
    with pytest.raises(ReplayError, match="AUDIO_OUTPUT_FAILED"):
        await AlsaOutput().play(tmp_path / "wake.wav")
    with pytest.raises(ReplayError, match="AUDIO_CAPTURE_FAILED"):
        await AlsaCapture().start()
    assert calls[0][0][0] == "aplay" and calls[1][0][0] == "arecord"
    assert all("shell" not in kwargs for _, kwargs in calls)


@pytest.mark.asyncio
async def test_missing_audio_binaries_are_named(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.device_replay.shutil.which", lambda name: None)
    with pytest.raises(ReplayError, match="AUDIO_OUTPUT_NOT_AVAILABLE"):
        await AlsaOutput().play(tmp_path / "wake.wav")
    with pytest.raises(ReplayError, match="AUDIO_CAPTURE_NOT_AVAILABLE"):
        await AlsaCapture().start()


@pytest.mark.asyncio
async def test_client_adapter_reads_public_events_turn_and_export():
    class FakeClient:
        async def events(self, satellite):
            yield {"event_id": 1, "event": event("wake_detected")}
            yield {"event_id": 2, "event": event("mic_open", 1000)}

        async def turn(self, turn_id):
            return {"turn_id": turn_id}

        async def export(self, satellite, since):
            yield {"event_id": 2, "event": event("mic_open", 1000)}

    source = ClientEvents(FakeClient(), "test_satellite")
    await source.start(10_500_000_000)
    assert (await source.next())["stage"] == "mic_open"
    assert await source.turn(TURN) == {"turn_id": TURN}
    assert (await source.export(10_500_000_000))[0]["stage"] == "mic_open"
    await source.close()


def review_signal(*, tail_ms=40, click=False, onset=False):
    reference = [0.0] * 800 + tone(4000, 0.5) + tone(tail_ms * 16, 0.005) + [0.0] * 1600
    boundary = 800 + 4000 + tail_ms * 16 - 1
    prefix = [0.0] * 800 + reference + [0.0] * 3200
    start = len(prefix)
    quiet = tone(2400, 0.004) if onset else []
    reply = quiet + tone(4800, 0.4, 419)
    mic = prefix + reply + [0.0] * 3200
    if click:
        mic[start - 1600 : start - 1120] = tone(480, 0.5, 917)
    return reference, mic, boundary, start


def test_labelled_sub_two_percent_tail_maps_through_fit():
    reference, mic, boundary, _ = review_signal()
    result = align_capture(Capture(mic, 16000, 2_000_000_000, 0), reference, boundary)
    mapped = result["command_offset_samples"] + boundary
    assert result["last_voice_sample"] == mapped
    assert (
        result["last_voice_utc_ns"] == 2_000_000_000 + mapped * 1_000_000_000 // 16000
    )
    assert result["uncertainty_ms"] >= 50


def test_sixty_ms_energy_disagreement_is_still_refused():
    reference, mic, boundary, _ = review_signal(tail_ms=60)
    with pytest.raises(ReplayError, match="ALIGNMENT_BOUNDARY"):
        align_capture(Capture(mic, 16000, 0, 0), reference, boundary)


def test_thirty_ms_click_before_reply_is_not_reply_start():
    reference, mic, boundary, start = review_signal(click=True)
    result = align_capture(Capture(mic, 16000, 0, 0), reference, boundary)
    assert result["reply_start_sample"] == start
    assert result["uncertainty_ms"] >= 100


def test_quiet_150_ms_reply_onset_is_measured_from_noise_floor():
    reference, mic, boundary, start = review_signal(onset=True)
    result = align_capture(Capture(mic, 16000, 0, 0), reference, boundary)
    assert result["reply_start_sample"] == start
    assert result["reply_end_sample"] == start + 2400 + 4800 - 1


@pytest.mark.parametrize("stage", DEVICE_STAGES[1:])
def test_duplicate_device_stage_has_duplicate_name(stage):
    row = sample()
    row["events"].append(copy.deepcopy(row[stage]))
    assert "DUPLICATE_DEVICE_" + stage.upper() in sample_errors(row)


@pytest.mark.parametrize(
    "earlier,later", list(zip(DEVICE_STAGES[:-1], DEVICE_STAGES[1:], strict=True))
)
def test_device_stage_order_is_required(earlier, later):
    row = sample()
    row[later]["t_utc_ns"] = row[earlier]["t_utc_ns"] - 1
    assert "DEVICE_EVENT_ORDER" in sample_errors(row)


def test_playback_command_time_is_not_an_acoustic_start():
    row = sample()
    row["host_timing"]["command_play_requested_utc_ns"] = row["alignment"][
        "reply_start_utc_ns"
    ]
    assert "NON_ACOUSTIC_ALIGNMENT" in sample_errors(row)


@pytest.mark.parametrize("method", ["synthetic", "playback-command", "unknown"])
def test_non_acoustic_alignment_method_is_refused(method):
    row = sample()
    row["alignment"]["method"] = method
    assert "NON_ACOUSTIC_ALIGNMENT" in sample_errors(row)


@pytest.mark.asyncio
async def test_playback_timeout_has_audio_output_error(monkeypatch, tmp_path):
    class Process:
        returncode = None
        calls = 0

        async def wait(self):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError
            self.returncode = -9
            return -9

        def kill(self):
            self.returncode = -9

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr("tools.device_replay.shutil.which", lambda name: name)
    monkeypatch.setattr("tools.device_replay.asyncio.create_subprocess_exec", spawn)
    label, recording = bundle(tmp_path)
    try:
        await replay_sample(
            label,
            tmp_path,
            tmp_path / "wake.wav",
            AlsaOutput(),
            FakeCapture(recording),
            FakeEvents(),
        )
    except Exception as error:
        assert isinstance(error, ReplayError)
        assert str(error) == "AUDIO_OUTPUT_FAILED"
    else:
        pytest.fail("playback timeout was not refused")


def test_acoustic_document_does_not_claim_peak_threshold_keeps_quiet_tails():
    document = (ROOT / "docs/testing/device-replay.md").read_text()
    assert (
        "A low relative threshold keeps quiet tails above the measured noise floor"
        not in document
    )


@pytest.mark.asyncio
async def test_hash_error_includes_corpus_failure_text(tmp_path):
    label, recording = bundle(tmp_path)
    label["audio_sha256"] = "0" * 64
    with pytest.raises(ReplayError, match="audio hash mismatch"):
        await replay_sample(
            label,
            tmp_path,
            tmp_path / "wake.wav",
            FakeOutput(),
            FakeCapture(recording),
            FakeEvents(),
        )


@pytest.mark.asyncio
async def test_client_protocol_failure_has_named_device_error(tmp_path):
    from assist_live_client.protocol import ProtocolError

    class BrokenEvents(FakeEvents):
        async def next(self):
            raise ProtocolError("event stream stopped")

    label, recording = bundle(tmp_path)
    capture, source = FakeCapture(recording), BrokenEvents()
    try:
        await replay_sample(
            label, tmp_path, tmp_path / "wake.wav", FakeOutput(), capture, source
        )
    except Exception as error:
        assert isinstance(error, ReplayError)
        assert str(error) == "MISSING_DEVICE_EVENT"
    else:
        pytest.fail("protocol failure was not refused")
    assert capture.aborted and source.closed


@pytest.mark.asyncio
async def test_capture_origin_is_first_pcm_byte_with_short_period(monkeypatch):
    clock = {"utc": 1_000_000_000}
    finished = asyncio.Event()
    calls = []

    class Output:
        calls = 0

        async def read(self, count):
            self.calls += 1
            if self.calls == 1:
                clock["utc"] += 80_000_000
                return b"\0\0" * 160
            await finished.wait()
            return b""

    class Process:
        returncode = None
        stdout = Output()

        def terminate(self):
            self.returncode = -15
            finished.set()

        def kill(self):
            self.terminate()

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr("tools.device_replay.time.time_ns", lambda: clock["utc"])
    monkeypatch.setattr("tools.device_replay.shutil.which", lambda name: name)
    monkeypatch.setattr("tools.device_replay.asyncio.create_subprocess_exec", spawn)
    capture = AlsaCapture()
    await capture.start()
    result = await capture.finish()
    assert result.start_utc_ns == 1_080_000_000
    assert result.uncertainty_ms == 20
    assert "--period-size=160" in calls[0]
    assert "--buffer-size=320" in calls[0]
    assert not capture.pcm


def measured_clock(offset_ns=0, uncertainty_ms=0):
    return {
        "kind": "measured",
        "offset_ns": offset_ns,
        "uncertainty_ms": uncertainty_ms,
        "source": "synthetic-ptp-report",
    }


@pytest.mark.asyncio
async def test_replay_records_server_clock_and_charges_uncertainty(tmp_path):
    label, recording = bundle(tmp_path)
    source = FakeEvents()
    source.server_clock = measured_clock(250_000_000, 7)
    row = await replay_sample(
        label,
        tmp_path,
        tmp_path / "wake.wav",
        FakeOutput(),
        FakeCapture(recording),
        source,
    )
    assert row.get("server_clock") == source.server_clock
    assert row["alignment"].get("clock_uncertainty_ms") == 7
    assert row["alignment"]["uncertainty_ms"] >= 4 + 10 + 50 + 7
    assert (
        source.since
        == row["host_timing"]["mark_start_utc_ns"] + 250_000_000 - 7_000_000
    )


def test_schema_refuses_missing_clock_metadata():
    row = sample()
    row.pop("server_clock", None)
    assert "INVALID_SERVER_CLOCK" in sample_errors(row)


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"kind": "measured", "offset_ns": 1, "uncertainty_ms": -1, "source": "ptp"},
        {"kind": "measured", "offset_ns": 1, "uncertainty_ms": 1, "source": ""},
        {
            "kind": "same_host",
            "offset_ns": 1,
            "uncertainty_ms": 0,
            "source": "local-interface",
        },
    ],
)
def test_schema_refuses_invalid_server_clock(invalid):
    row = sample()
    row["server_clock"] = invalid
    assert "INVALID_SERVER_CLOCK" in sample_errors(row)


def test_same_host_clock_proves_all_resolved_addresses_are_local(tmp_path, monkeypatch):
    from tools.device_replay import read_server_clock

    path = tmp_path / "clock.json"
    path.write_text(json.dumps({"kind": "same_host"}))
    monkeypatch.setattr(
        "tools.device_replay.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 80))],
    )
    result = read_server_clock(path, "https://example.com")
    assert result["offset_ns"] == 0 and result["uncertainty_ms"] == 0
    monkeypatch.setattr("tools.device_replay.local_addresses", lambda: {"192.0.2.10"})
    monkeypatch.setattr(
        "tools.device_replay.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("192.0.2.10", 80))],
    )
    assert read_server_clock(path, "https://example.com")["kind"] == "same_host"
    monkeypatch.setattr(
        "tools.device_replay.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 80)),
            (2, 1, 6, "", ("192.0.2.11", 80)),
        ],
    )
    with pytest.raises(ReplayError, match="SERVER_CLOCK_NOT_LOCAL"):
        read_server_clock(path, "https://example.com")


def test_measured_clock_requires_named_nonnegative_uncertainty(tmp_path):
    from tools.device_replay import read_server_clock

    path = tmp_path / "clock.json"
    record = measured_clock(250_000_000, 3)
    path.write_text(json.dumps(record))
    assert read_server_clock(path, "https://example.com") == record
    for bad in [
        {**record, "uncertainty_ms": -1},
        {**record, "source": ""},
        {"kind": "unknown"},
    ]:
        path.write_text(json.dumps(bad))
        with pytest.raises(ReplayError, match="INVALID_SERVER_CLOCK"):
            read_server_clock(path, "https://example.com")


def test_local_interface_addresses_use_kernel_interfaces(monkeypatch):
    import errno

    from tools.device_replay import local_addresses

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fileno(self):
            return 42

    monkeypatch.setattr("tools.device_replay.socket.socket", lambda *args: Socket())
    monkeypatch.setattr(
        "tools.device_replay.socket.if_nameindex", lambda: [(1, "lo"), (2, "test0")]
    )

    def interface_address(fd, operation, request):
        assert fd == 42 and operation == 0x8915
        if request.startswith(b"lo\0"):
            raise OSError(errno.EADDRNOTAVAIL, "no IPv4")
        return b"\0" * 20 + bytes((192, 0, 2, 10)) + b"\0" * 232

    monkeypatch.setattr("tools.device_replay.fcntl.ioctl", interface_address)
    monkeypatch.setattr("tools.device_replay.Path.exists", lambda path: True)
    monkeypatch.setattr(
        "tools.device_replay.Path.read_text",
        lambda path: "20010db8000000000000000000000001 02 40 00 80 test0\n",
    )
    assert local_addresses() == {"192.0.2.10", "2001:db8::1"}


def test_local_interface_proof_errors_fail_closed(tmp_path, monkeypatch):
    from tools.device_replay import read_server_clock

    path = tmp_path / "clock.json"
    path.write_text('{"kind":"same_host"}')
    monkeypatch.setattr(
        "tools.device_replay.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("192.0.2.10", 80))],
    )

    def unavailable():
        raise ReplayError("SERVER_CLOCK_LOCAL_PROOF_UNAVAILABLE")

    monkeypatch.setattr("tools.device_replay.local_addresses", unavailable)
    with pytest.raises(ReplayError, match="SERVER_CLOCK_LOCAL_PROOF_UNAVAILABLE"):
        read_server_clock(path, "https://example.com")
    with pytest.raises(ReplayError, match="SERVER_CLOCK_NOT_AVAILABLE"):
        read_server_clock(None, "https://example.com")


def test_device_stage_equal_timestamps_are_valid():
    row = sample()
    for item in row["events"]:
        item["t_utc_ns"] = 10_000_000_000
    assert validate_sample(row)


@pytest.mark.asyncio
async def test_client_event_timeout_is_missing_device_event(monkeypatch):
    class Client:
        async def events(self, satellite):
            raise TimeoutError
            yield

    source = ClientEvents(Client(), "test_satellite")
    await source.start(0)
    try:
        await source.next()
    except Exception as error:
        assert isinstance(error, ReplayError)
        assert str(error) == "MISSING_DEVICE_EVENT"
    else:
        pytest.fail("event timeout was not refused")
    await source.close()


def test_alignment_uncertainty_uses_the_shared_boundary_tolerance(
    tmp_path, monkeypatch
):
    from tools import corpus, device_replay

    label, recording = bundle(tmp_path)
    reference, _ = device_replay.load_audio(tmp_path / label["audio_path"])
    before = align_capture(recording, reference, label["last_voice_sample"])
    monkeypatch.setattr(corpus, "BOUNDARY_TOLERANCE_MS", 100)
    monkeypatch.setattr(device_replay, "BOUNDARY_TOLERANCE_MS", 100)
    after = align_capture(recording, reference, label["last_voice_sample"])
    assert after["uncertainty_ms"] == before["uncertainty_ms"] + 50


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_failure", [False, True])
async def test_one_failed_attempt_of_three_is_written_and_not_dropped(
    tmp_path, monkeypatch, replace_failure
):
    labels = [
        {
            "id": f"clip-{i}",
            "audio_sha256": "a" * 64,
            "split": "held_out",
            "set": "replay",
            "intent": {"name": "HassTurnOn", "slots": {"name": "virtual light"}},
        }
        for i in range(3)
    ]

    async def capture_loss_without_network(*args, **kwargs):
        from tools.measurement import capture_loss

        return await capture_loss(None, satellites=kwargs["satellites"])

    monkeypatch.setattr(
        "tools.device_replay.capture_loss", capture_loss_without_network
    )
    token, records_path = tmp_path / "token.json", tmp_path / "operations.jsonl"
    token.write_text(json.dumps({"token": "test-only"}))
    for key, value in {
        "ASSIST_LIVE_URL": "http://127.0.0.1",
        "ASSIST_LIVE_TOKEN_FILE": str(token),
        "ASSIST_LIVE_SATELLITE": "test_satellite",
        "ASSIST_LIVE_WAKE_AUDIO": "wake.wav",
        "ASSIST_LIVE_RUN_ID": "test-run",
        "ASSIST_LIVE_BASELINE_CONFIG_ID": "config-sha256-01",
        "ASSIST_LIVE_CALIBRATION_REVISION": "calibration-01",
        "ASSIST_LIVE_OPERATION_RECORDS": str(records_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("tools.device_replay.frozen_corpus", lambda *args: True)
    monkeypatch.setattr(
        "tools.device_replay.load_corpus",
        lambda path: (
            labels
            if path.name == "labels.jsonl"
            else [json.loads(line) for line in records_path.read_text().splitlines()]
        ),
    )
    monkeypatch.setattr(
        "tools.device_replay.read_server_clock", lambda *args: sample()["server_clock"]
    )

    class Client:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    records = []

    async def replay(label, *args):
        if label["id"] == "clip-1":
            raise ReplayError("MISSING_DEVICE_PLAYBACK_END")
        row = sample()
        row["clip_id"] = label["id"]
        row["turn_id"] = f"01900000-0000-7000-8000-{int(label['id'][-1]) + 1:012x}"
        for item in row["events"]:
            item["turn_id"] = row["turn_id"]
        records.append(recorder(row))
        records_path.write_text(
            "".join(json.dumps(record) + "\n" for record in records)
        )
        return row

    monkeypatch.setattr("tools.device_replay.AssistLiveClient", Client)
    monkeypatch.setattr("tools.device_replay.replay_sample", replay)
    args = options()
    args.labels, args.output = tmp_path / "labels.jsonl", tmp_path / "runs.jsonl"
    if replace_failure:
        replace = Path.replace

        def reject_second(source, target):
            if target == args.output and args.output.exists():
                assert source.parent == args.output.parent
                assert source.stat().st_mode & 0o777 == 0o600
                raise OSError(errno.ENOSPC, "synthetic replacement failure")
            return replace(source, target)

        monkeypatch.setattr(Path, "replace", reject_second)
        with pytest.raises(OSError, match="synthetic replacement failure"):
            await run(args)
        checkpoint = json.loads(args.output.read_text())
        assert checkpoint["clip_id"] == "clip-0"
        assert not checkpoint["failures"]
        assert not checkpoint["complete"]
        assert not list(tmp_path.glob("*.tmp"))
        return
    previous = os.umask(0o022)
    try:
        rows = await run(args)
    finally:
        os.umask(previous)
    assert args.output.stat().st_mode & 0o777 == 0o600, "PRIVATE_REPLAY_MODE"
    assert len(rows) == 3
    assert [row["complete"] for row in rows] == [True, False, True]
    assert "MISSING_DEVICE_PLAYBACK_END" in rows[1]["failures"]
    assert rows[1]["action_trace_complete"] is False
    assert len(args.output.read_text().splitlines()) == 3


def test_bridge_operations_cannot_disagree_with_public_actions():
    row = sample()
    record = recorder(row)
    record["service_calls"][0].update(
        domain="fan", service="turn_off", service_data={"entity_id": "fan.virtual_fan"}
    )
    record["actions"][0] = {"kind": "service", **record["service_calls"][0]}
    with pytest.raises(ReplayError, match="INVALID_OPERATION_EVIDENCE"):
        attach_evidence(row, {"intent": row["evaluated_intent"]}, [record])
