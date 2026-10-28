import asyncio
import copy
import hashlib
import json
import math
import random
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
    UnavailableProfileSelector,
    align_capture,
    attach_evidence,
    baseline_for,
    check_options,
    frozen_corpus,
    parser,
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
        "events": events,
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
    assert check_options(options("--limit", "2")) == ["baseline"]


@pytest.mark.parametrize(
    "extra,error",
    [
        (["--profiles", "safe,fast"], "PROFILES_P1_ONLY"),
        (["--phase", "p1"], "P1_REQUIRES_SAFE_FAST"),
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


def bundle(tmp_path):
    reference, mic = signals()
    path = tmp_path / "command.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(
            b"".join(struct.pack("<h", round(value * 32767)) for value in reference)
        )
    label = {
        "id": "clip-01",
        "set": "replay",
        "split": "held_out",
        "audio_path": path.name,
        "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sample_rate": 16000,
        "last_voice_sample": 7999,
        "intent": {"name": "HassTurnOn", "slots": {"name": "virtual light"}},
    }
    return label, Capture(mic, 16000, 10_000_000_000, 4)


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
    args.labels = labels
    with pytest.raises(ReplayError, match="BLOCKED corpus not frozen"):
        await run(args)
    args.limit = 2
    with pytest.raises(ReplayError, match="REPLAY_SETUP_NOT_AVAILABLE"):
        await run(args)
    assert "NOT FROZEN corpus" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_real_profile_selector_is_explicitly_unavailable():
    with pytest.raises(ReplayError, match="NOT AVAILABLE: device profile selector"):
        await UnavailableProfileSelector().select("safe")


@pytest.mark.asyncio
async def test_fake_selector_runs_both_p1_profiles_and_p0_without_selection(
    tmp_path, monkeypatch
):
    label, _ = bundle(tmp_path)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps(label) + "\n")
    records_path = tmp_path / "operations.jsonl"
    baseline = sample()
    baseline.update(clip_id=label["id"], audio_sha256=label["audio_sha256"])
    baselines = tmp_path / "baseline.jsonl"
    baselines.write_text(json.dumps(baseline) + "\n")
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token": "test-only"}))
    clock_record = tmp_path / "clock.json"
    clock_record.write_text(json.dumps({"kind": "same_host"}))
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

    class RecordingEvents(FakeEvents):
        def __init__(self, *args):
            turn = f"01900000-0000-7000-8000-{len(records) + 10:012x}"
            items = [
                event(stage, i * 1000, turn) for i, stage in enumerate(DEVICE_STAGES)
            ]
            transcript = event("stt_final", 1500, turn, "server")
            transcript["data"] = {"transcript": baseline["recognized_transcript"]}
            super().__init__(items, items + [transcript])
            row = copy.deepcopy(baseline)
            row["turn_id"] = turn
            records.append(recorder(row))

        async def close(self):
            await super().close()
            records_path.write_text(
                "".join(json.dumps(record) + "\n" for record in records)
            )

    class Selector:
        def __init__(self):
            self.calls = []

        async def select(self, profile):
            self.calls.append(profile)

    monkeypatch.setattr("tools.device_replay.AssistLiveClient", FakeClient)
    monkeypatch.setattr("tools.device_replay.AlsaOutput", lambda *args: FakeOutput())
    monkeypatch.setattr(
        "tools.device_replay.AlsaCapture",
        lambda *args: FakeCapture(bundle(tmp_path)[1]),
    )
    monkeypatch.setattr("tools.device_replay.ClientEvents", RecordingEvents)
    args = options("--phase", "p1", "--profiles", "safe,fast", "--limit", "1")
    args.labels, args.audio_root = labels, tmp_path
    args.output = tmp_path / "runs.jsonl"
    args.clock_record = clock_record
    selector = Selector()
    rows = await run(args, selector)
    assert all(row["mode"] == "smoke" for row in rows)
    assert selector.calls == ["safe", "fast"]
    assert [row["profile"] for row in rows] == ["safe", "fast"]
    assert all(
        row["baseline_transcript"] == baseline["recognized_transcript"] for row in rows
    )
    assert all(
        validate_sample(row, acceptance=False) and not row["frozen"] for row in rows
    )
    with pytest.raises(ReplayError, match="STALE_OPERATION_EVIDENCE"):
        await run(args, Selector())

    records_path.unlink()
    selector.calls.clear()
    args.phase, args.profiles = "p0", None
    rows = await run(args)
    assert all(row["mode"] == "smoke" for row in rows)
    assert [row["profile"] for row in rows] == ["baseline"]
    assert selector.calls == []


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
async def test_one_failed_attempt_of_three_is_written_and_not_dropped(
    tmp_path, monkeypatch
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
    rows = await run(args)
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
