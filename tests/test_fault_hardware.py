import asyncio
import copy
import hashlib
import importlib
import random
import statistics
import struct
import wave
from argparse import Namespace
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_fault_runner import trial
from tools import fault_runner


def hardware_module():
    assert importlib.util.find_spec("tools.fault_hardware"), "HARDWARE_DRIVER_REQUIRED"
    return importlib.import_module("tools.fault_hardware")


def wav_file(path, pcm):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(pcm)
    return path


def arguments(tmp_path):
    return Namespace(
        hardware=True,
        satellite="assist_satellite.test",
        playback_sink="test-sink",
        wake_audio=wav_file(tmp_path / "wake.wav", bytes(3200)),
        ha_container="test-ha",
        turn_audio_archive=tmp_path / "archive",
    )


@pytest.mark.parametrize(
    "missing",
    ["satellite", "playback_sink", "wake_audio", "ha_container", "turn_audio_archive"],
)
def test_hardware_cli_requires_every_explicit_argument(missing, tmp_path, capsys):
    options = {
        "--satellite": "assist_satellite.test",
        "--playback-sink": "test-sink",
        "--wake-audio": str(tmp_path / "wake.wav"),
        "--ha-container": "test-ha",
        "--turn-audio-archive": str(tmp_path / "archive"),
    }
    options.pop("--" + missing.replace("_", "-"))
    argv = [
        "--hardware",
        "--output",
        str(tmp_path / "result.json"),
        "--token-file",
        "authentication.json",
        "--pipeline",
        "test",
        "--audio-root",
        str(tmp_path),
        "--temporary-agent-pipeline",
        *(value for pair in options.items() for value in pair),
    ]
    with pytest.raises(SystemExit) as error:
        fault_runner.main(argv)
    assert error.value.code == 2, "MISSING_HARDWARE_ARGUMENT_REFUSED"
    assert "hardware requires" in capsys.readouterr().err, "EXPLICIT_ARGUMENT_ERROR"


@pytest.fixture
def satellite_bench(tmp_path):
    calls = []
    states = {
        "assist_satellite.test": "idle",
        "switch.mute": "off",
        "select.pipeline": "Original",
    }
    registry = [
        {"entity_id": entity, "device_id": "physical", "translation_key": key}
        for entity, key in (
            ("assist_satellite.test", None),
            ("switch.mute", "microphone"),
            ("select.pipeline", "pipeline"),
        )
    ]

    async def command(kind, **fields):
        calls.append((kind, fields))
        if kind == "get_states":
            return [
                {"entity_id": entity, "state": value, "attributes": {}}
                for entity, value in states.items()
            ]
        if kind == "config/entity_registry/list":
            return registry
        if kind == "assist_pipeline/device/list":
            return [{"device_id": "physical", "pipeline_entity": "select.pipeline"}]
        if kind == "assist_pipeline/pipeline/get":
            return {"id": "temporary", "name": "Temporary"}
        if kind == "call_service":
            assert fields["domain"] == "select", "MUTE_NEVER_CHANGED"
            states[fields["target"]["entity_id"]] = fields["service_data"]["option"]
            return None
        raise AssertionError(kind)

    return hardware_module().Hardware(arguments(tmp_path)), command, states, calls


@pytest.mark.parametrize("muted", ["on", "unavailable", "unknown"])
async def test_muted_or_unknown_satellite_refused_without_change(
    satellite_bench, muted
):
    hardware, command, states, calls = satellite_bench
    states["switch.mute"] = muted
    with pytest.raises(ValueError, match="microphone"):
        await hardware.preflight(command)
    assert not any(kind == "call_service" for kind, _ in calls), "MUTED_NO_CHANGES"


@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "select-error"])
async def test_satellite_pipeline_restore_on_every_path(satellite_bench, outcome):
    hardware, command, states, calls = satellite_bench
    await hardware.preflight(command)
    result = {}

    async def select_command(kind, **fields):
        value = await command(kind, **fields)
        if (
            outcome == "select-error"
            and kind == "call_service"
            and fields["service_data"]["option"] == "Temporary"
        ):
            raise ValueError("select failed after apply")
        return value

    async def execute():
        async with hardware.selected(select_command, "temporary", result):
            assert states["select.pipeline"] == "Temporary"
            if outcome == "error":
                raise ValueError("synthetic error")
            if outcome == "cancel":
                raise asyncio.CancelledError

    if outcome == "success":
        await execute()
    else:
        with pytest.raises(
            asyncio.CancelledError if outcome == "cancel" else ValueError
        ):
            await execute()
    assert states["select.pipeline"] == "Original", "PIPELINE_RESTORED_ON_ERROR"
    assert result["satellite_pipeline_restored"] is True, "RESTORE_READ_BACK"
    assert calls[-1][0] == "get_states", "RESTORE_NOT_ONLY_REQUESTED"


def hardware_row():
    row = trial()
    row.update(
        scope="hardware",
        hardware_invalid_reason=None,
        correlation={"offset_samples": 23000, "complete": True},
        server_audio_samples=47000,
        server_audio_last_chunk_samples=320,
    )
    row["events"].append({"stage": "run-end", "t_utc_ns": 20_000_000_000})
    return row


@pytest.mark.parametrize(
    "sent,last,expected",
    [(47000, 320, (23680, 24000)), (23000, 0, (0, 0))],
)
def test_fault_interval_subtracts_correlation_offset(sent, last, expected):
    hardware = hardware_module()
    assert hardware.fault_interval(sent, last, 23000) == expected, "OFFSET_REQUIRED"


@pytest.mark.parametrize(
    "sent,last",
    [(True, 0), (-1, 0), (10, 11), (10, -1), (10, None)],
)
def test_unknown_sample_interval_is_unavailable(sent, last):
    assert hardware_module().fault_interval(sent, last, 1) is None


@pytest.mark.parametrize(
    "interval,expected",
    [
        ((16000, 16320), "verified"),
        ((31679, 31999), "verified"),
        ((15999, 16320), "invalid_injection"),
        ((31680, 32000), "invalid_injection"),
    ],
)
def test_hardware_window_checks_both_interval_edges(interval, expected):
    row = hardware_row()
    row.update(
        server_audio_samples=23000 + interval[1],
        server_audio_last_chunk_samples=interval[1] - interval[0],
        fault_stopped_audio_sample=24000,
    )
    assert fault_runner.injection_status(row) == expected, "WHOLE_INTERVAL_IN_WINDOW"


@pytest.mark.parametrize("reason", ["no_wake", "early_end", "fault_outside_window"])
def test_invalid_try_is_replaced_without_reading_outcome(reason):
    for correct in (True, False):
        row = hardware_row()
        row["hardware_invalid_reason"] = reason
        if not correct:
            row["recognized_transcript"] = "wrong command"
        assert fault_runner.injection_status(row) == "invalid_injection", (
            "OUTCOME_BLIND_HARDWARE_REPLACEMENT"
        )


def test_missing_correlation_is_not_a_success_or_replacement():
    row = hardware_row()
    row["correlation"] = None
    assert fault_runner.injection_status(row) == "unavailable", "MISSING_PROOF_FAILS"
    assert not fault_runner.acceptable(row)


def test_hardware_acceptance_uses_media_interval_not_host_fault_sample():
    row = hardware_row()
    row["fault_stopped_audio_sample"] = 999999
    assert fault_runner.acceptable(row), "HARDWARE_MEDIA_POSITION_IS_AUTHORITATIVE"


@pytest.mark.parametrize("fallback", ["tts", "stt", "error"])
def test_hardware_after_final_uses_own_stt_cause_and_requires_complete_reply(fallback):
    row = hardware_row()
    row.update(
        stage="after-final",
        failure_cause=None,
        fallback_reasons_after={"health_error": 1},
        fallback_reasons_delta={"health_error": 1},
        tts_failure_cause="health_error" if fallback == "tts" else None,
    )
    row["fallback_turns"][0]["destinations"] = (
        [{"part": fallback + "_fallback", "fallback_reason": "health_error"}]
        if fallback in ("tts", "stt")
        else []
    )
    if fallback == "stt":
        row["failure_cause"] = "health_error"
    if fallback == "error":
        row["fallback_reasons_after"] = {}
        row["fallback_reasons_delta"] = {}
        row["events"].insert(1, {"stage": "error", "data": {"code": "tts_failed"}})
    assert fault_runner.acceptable(row) is (fallback == "tts"), {
        "tts": "AFTER_FINAL_TTS_FALLBACK_IS_CORRECT",
        "stt": "AFTER_FINAL_STT_FALLBACK_IS_INCORRECT",
        "error": "REPLY_ERROR_FAILS_HARDWARE_TRIAL",
    }[fallback]


@pytest.mark.parametrize("aggregate", [None, {}, {"unrelated": 17}])
def test_hardware_uses_own_turn_cause_even_if_aggregate_is_unavailable(aggregate):
    row = hardware_row()
    row.update(
        fallback_reasons_before=aggregate,
        fallback_reasons_after=aggregate,
        fallback_reasons_delta=aggregate,
    )
    assert fault_runner.acceptable(row), "HARDWARE_AGGREGATE_IS_NOT_CAUSE_EVIDENCE"


def test_correlation_and_early_turn_detection():
    hardware = hardware_module()
    rng = random.Random(1379)
    source = [rng.randrange(-12000, 12000) for _ in range(12000)]
    reference = struct.pack("<12000h", *source)
    recording = bytes(1379 * 2) + struct.pack("<12000h", *[-n for n in source])
    result = hardware.correlate(recording + bytes(2000), reference)
    assert abs(result["offset_samples"] - 1379) <= 4, "CORRELATION_KNOWN_OFFSET"
    assert result["complete"] is True, "FULL_RECORDING_FOUND"
    early = hardware.correlate(recording[:-6000], reference)
    assert early["complete"] is False, "EARLY_END_IS_INVALID"
    assert (
        hardware.correlate(bytes(len(recording)), reference)["offset_samples"] is None
    )


async def test_per_trial_baseline_uses_archived_turn_not_source(tmp_path, monkeypatch):
    hardware = hardware_module().Hardware(arguments(tmp_path))
    pcm = struct.pack("<100h", *range(100))
    path = wav_file(tmp_path / "turn.wav", pcm)
    row = hardware_row()
    row["turn_audio"] = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    label = {"id": "test", "audio_sha256": "a" * 64}
    calls = []

    async def baselines(*args, **kwargs):
        calls.append(args)
        assert args[4] == pcm, "BASELINE_ON_TURN_AUDIO_NOT_SOURCE"
        assert args[3]["audio_sha256"] == row["turn_audio"]["sha256"]
        return copy.deepcopy(trial()["baselines"])

    monkeypatch.setattr(fault_runner, "collect_baselines", baselines)
    await hardware.baselines(
        None,
        tmp_path / "auth",
        "pipeline",
        label,
        b"source",
        {},
        None,
        row,
        "sensor.turn",
    )
    assert len(calls) == 1, "BASELINE_EACH_TRIAL"
    assert row["source_audio_sha256"] == label["audio_sha256"]
    assert row["audio_sha256"] == row["turn_audio"]["sha256"]


async def test_archive_copy_is_private_hashed_and_never_deletes(tmp_path):
    hardware = hardware_module().Hardware(arguments(tmp_path))
    source = wav_file(tmp_path / "retained-source.wav", bytes(1000))
    payload = source.read_bytes()
    calls = []

    class Docker:
        async def docker(self, *args):
            calls.append(args)
            if args[0] == "cp":
                __import__("pathlib").Path(args[2]).write_bytes(payload)
                return ""
            assert args[0] == "exec", "ARCHIVE_COPY_NEVER_DELETE"
            return hashlib.sha256(payload).hexdigest()

    record = await hardware.archive(
        Docker(),
        "/config/pipeline_recordings/device/pipeline/1/01_stt-test.wav",
        "turn-id",
        "run-id",
    )
    archived = __import__("pathlib").Path(record["path"])
    assert archived.read_bytes() == payload, "ARCHIVE_COPY_COMPLETE"
    assert archived.stat().st_mode & 0o777 == 0o600, "ARCHIVE_MODE_600"
    assert archived.parent.stat().st_mode & 0o777 == 0o700, "ARCHIVE_PRIVATE"
    assert record["sha256"] == hashlib.sha256(payload).hexdigest()
    assert source.read_bytes() == payload, "SOURCE_AUDIO_RETAINED"
    assert all(args[0] in ("exec", "cp") for args in calls), "ARCHIVE_COPY_NEVER_DELETE"


def test_installed_ha_debug_list_get_and_satellite_contract():
    from homeassistant.components.assist_pipeline import websocket_api
    from homeassistant.components.assist_pipeline.models import (
        PipelineEvent,
        PipelineEventType,
    )
    from homeassistant.components.assist_pipeline.runtime import KEY_ASSIST_PIPELINE

    event = PipelineEvent(
        PipelineEventType.RUN_START, {"satellite_id": "assist_satellite.test"}
    )
    hass = SimpleNamespace(
        data={
            KEY_ASSIST_PIPELINE: SimpleNamespace(
                pipeline_debug={
                    "pipeline": {
                        "run-id": SimpleNamespace(
                            timestamp="2026-01-01T00:00:00+00:00", events=[event]
                        )
                    },
                }
            )
        }
    )
    connection = Mock()
    connection.user.is_admin = True
    websocket_api.websocket_list_runs(
        hass, connection, {"id": 1, "pipeline_id": "pipeline"}
    )
    response = connection.send_result.call_args.args[1]
    assert response["pipeline_runs"][0]["pipeline_run_id"] == "run-id", (
        "DEBUG_RUN_ID_CONTRACT"
    )
    websocket_api.websocket_get_run(
        hass,
        connection,
        {"id": 2, "pipeline_id": "pipeline", "pipeline_run_id": "run-id"},
    )
    assert connection.send_result.call_args.args[1] == {"events": [event]}, (
        "DEBUG_EVENTS_CONTRACT"
    )
    import inspect

    from homeassistant.components.assist_pipeline.run import PipelineRun

    source = inspect.getsource(PipelineRun.start)
    assert 'data["satellite_id"] = satellite_id' in source, (
        "DEBUG_SATELLITE_ID_CONTRACT"
    )


@pytest.mark.parametrize(
    "outcome",
    [
        "valid",
        "no-wake",
        "early",
        "ambiguous-file",
        "ambiguous-run",
        "wrong-satellite",
        "missing-turn",
    ],
)
@pytest.mark.parametrize("stage", ["before", "speech", "pause", "after-final"])
async def test_hardware_trial_links_ids_and_keeps_continuous_audio(
    tmp_path, monkeypatch, outcome, stage
):
    module = hardware_module()
    hardware = module.Hardware(arguments(tmp_path))
    hardware.device_id = "physical"
    hardware.pipeline_name = "Temporary"
    hardware.mute_entity = "switch.mute"
    hardware.delay_samples = [0.1]
    state = {"played": False, "ended": False, "gets": 0, "archives": 0}
    records = asyncio.Queue()
    original_sleep = asyncio.sleep
    source = struct.pack("<1200h", *[n % 100 for n in range(1200)])
    turn = wav_file(tmp_path / "captured.wav", bytes(400) + source + bytes(400))
    calls = []

    async def pause(_):
        await original_sleep(0)

    async def command(kind, **fields):
        await original_sleep(0)
        if kind == "get_states":
            return [
                {"entity_id": "switch.mute", "state": "off"},
                {"entity_id": "assist_satellite.test", "state": "idle"},
                {
                    "entity_id": "sensor.turn",
                    "attributes": {
                        "turn_type": "voice",
                        "turn_id": "turn-new"
                        if state["ended"] and outcome != "missing-turn"
                        else "previous",
                    },
                },
            ]
        if kind.endswith("/list"):
            ids = ["old"]
            if state["played"] and outcome != "no-wake":
                ids += ["run-new"]
                if outcome == "ambiguous-run":
                    ids += ["other-new"]
            return {"pipeline_runs": [{"pipeline_run_id": value} for value in ids]}
        if kind.endswith("/get"):
            assert fields["pipeline_run_id"] == "run-new", "DEBUG_FETCH_BY_RUN_ID"
            state["gets"] += 1
            events = [
                {
                    "type": "run-start",
                    "data": {
                        "satellite_id": "assist_satellite.other"
                        if outcome == "wrong-satellite"
                        else hardware.satellite
                    },
                },
                {"type": "stt-start"},
                {
                    "type": "stt-end",
                    "data": {"stt_output": {"text": "test transcript"}},
                },
            ]
            if state["gets"] > 1:
                state["ended"] = True
                events += [{"type": "intent-end"}, {"type": "run-end"}]
                await records.put(
                    {
                        "event": {
                            "event_type": "assist_live_test_record",
                            "data": {
                                "turn_id": "turn-new",
                                "device_id": "physical",
                                "recognized_transcript": "test transcript",
                                "completed": True,
                                "selected_intent": {"name": "Test", "slots": {}},
                                "operations": [],
                            },
                        }
                    }
                )
                await original_sleep(0)
            return {
                "events": [
                    {"timestamp": "2026-01-01T00:00:00+00:00", **event}
                    for event in events
                ]
            }
        raise AssertionError(kind)

    @asynccontextmanager
    async def control(*_):
        yield command

    class Socket:
        first = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def send_json(self, _):
            pass

        async def receive_json(self):
            if self.first:
                self.first = False
                return {"success": True}
            return await records.get()

    async def connect(*_):
        return Socket()

    class Process:
        returncode = 0

    async def play(*args, **kwargs):
        state["played"] = True
        path = Path(args[-1])
        assert module.read_pcm(path) == hardware.wake + source, "WAKE_AND_CLIP_NO_GAP"
        calls.append("play")
        return Process()

    class Server:
        async def stop(self):
            if stage == "after-final":
                assert state["gets"] >= 1, "AFTER_FINAL_USES_PUBLIC_FINAL"
            calls.append("stop")

        async def restore(self):
            calls.append("restore")

    async def snapshot(*_):
        return {
            "fallback_reasons": {},
            "turns": [{"turn_id": "turn-new", "destinations": []}],
        }

    async def files(*_):
        if not state["ended"]:
            return {"old.wav"}
        return {
            "old.wav",
            "new.wav",
            *(["other.wav"] if outcome == "ambiguous-file" else []),
        }

    async def archive(server, path, turn_id, run_id):
        assert (path, turn_id, run_id) == ("new.wav", "turn-new", "run-new"), (
            "ARCHIVE_LINKED_TO_BOTH_IDS"
        )
        state["archives"] += 1
        return {
            "path": str(turn),
            "sha256": hashlib.sha256(turn.read_bytes()).hexdigest(),
        }

    monkeypatch.setattr(fault_runner, "control_command", control)
    monkeypatch.setattr(fault_runner, "connect", connect)
    monkeypatch.setattr(fault_runner, "fallback_snapshot", snapshot)
    monkeypatch.setattr(module.asyncio, "sleep", pause)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", play)
    monkeypatch.setattr(hardware, "files", files)
    monkeypatch.setattr(hardware, "archive", archive)
    monkeypatch.setattr(
        module,
        "correlate",
        lambda *_: {
            "offset_samples": 200,
            "complete": outcome != "early",
            "reference_samples": 1200,
        },
    )
    if outcome == "early":
        turn = wav_file(tmp_path / "early.wav", bytes(500))
    row = await hardware.trial(
        None,
        tmp_path / "auth",
        "pipeline",
        {
            "id": "clip",
            "audio_sha256": "a" * 64,
            "alignment_words": [{"start_sample": 16000, "end_sample": 32000}],
            "pause_start_sample": 32000,
            "pause_end_sample": 48000,
        },
        source,
        stage,
        Server(),
        stop_latency_seconds=0.1,
        stop_margin_seconds=0.02,
        sensor="sensor.turn",
    )
    assert calls[-1] == "restore", "SERVER_RESTORED_EVERY_TRIAL"
    assert list(hardware.directory.glob("*-playback.wav")), "PLAYBACK_AUDIO_RETAINED"
    if outcome in ("valid", "early"):
        assert row["turn_id"] == "turn-new", "TURN_FROM_SENSOR_NOT_TIME"
        assert row["pipeline_run_id"] == "run-new"
        assert state["archives"] == 1, "TURN_COPIED_IMMEDIATELY"
        assert (calls.index("stop") < calls.index("play")) is (stage == "before"), (
            "HARDWARE_STOP_ORDER"
        )
        assert row["action_trace_complete"], "OPERATION_RECORD_LINKED_BY_TURN"
        assert row["hardware_invalid_reason"] == (
            "early_end" if outcome == "early" else None
        )
    elif outcome == "no-wake":
        assert row["hardware_invalid_reason"] == "no_wake", "NO_WAKE_INVALID"
    else:
        assert state["archives"] == 0, "AMBIGUOUS_IDENTITY_REFUSED"
        assert fault_runner.injection_status(row) == "unavailable", (
            "AMBIGUOUS_UNAVAILABLE"
        )


@pytest.mark.parametrize(
    "reason", ["no_wake", "early_end", "fault_outside_window", None]
)
@pytest.mark.parametrize("correct", [True, False])
async def test_hardware_invalid_replacement_uses_shared_three_attempt_loop(
    tmp_path, monkeypatch, reason, correct
):
    module = hardware_module()
    label = {"id": "test", "split": "held_out", "audio_sha256": "a" * 64}
    calls = []

    class Hardware:
        device_id = "physical"
        delay_samples = []

        def __init__(self, _):
            pass

        async def preflight(self, _):
            pass

        @asynccontextmanager
        async def selected(self, *_):
            yield

        async def trial(self, *args, **kwargs):
            calls.append(args[3]["id"])
            row = hardware_row()
            row.update(
                stage="before", label=trial()["label"], hardware_invalid_reason=reason
            )
            if not correct:
                row["recognized_transcript"] = "wrong command"
            row["failures"] = []
            return row

        async def baselines(self, *_):
            pass

    @asynccontextmanager
    async def control(*_):
        yield None

    @asynccontextmanager
    async def temporary(*_, **__):
        yield "temporary", "virtual", "sensor.turn"

    async def restore(*_):
        pass

    async def latency(*_):
        return 0.1

    async def engines(*_):
        return trial()["stt_engines"]

    token = tmp_path / "auth.json"
    token.write_text('{"token":"synthetic"}')
    monkeypatch.setattr(module, "Hardware", Hardware)
    monkeypatch.setattr(fault_runner, "load_corpus", lambda _: [label])
    monkeypatch.setattr(fault_runner, "audio_input", lambda *_: b"source")
    monkeypatch.setattr(fault_runner, "control_command", control)
    monkeypatch.setattr(fault_runner, "temporary_pipeline", temporary)
    monkeypatch.setattr(fault_runner, "baseline_engines", engines)
    monkeypatch.setattr(fault_runner.TestServer, "restore", restore)
    monkeypatch.setattr(fault_runner.TestServer, "measure_stop_latency", latency)
    result = await fault_runner.run(
        Namespace(
            hardware=True,
            output=tmp_path / "result.json",
            simulate=False,
            check=True,
            labels=tmp_path / "labels.jsonl",
            native_matrix=None,
            stages="before",
            each=1,
            audio_root=tmp_path,
            token_file=token,
            temporary_agent_pipeline=True,
            pipeline="source",
        )
    )
    count = 3 if reason else 1
    assert calls == ["test"] * count, "HARDWARE_ATTEMPT_CAP_THREE"
    assert len(result["attempts"]) == count, "HARDWARE_INVALID_ATTEMPTS_RETAINED"
    assert len(result["trials"]) == 1, "HARDWARE_ONE_SLOT"
    assert result["trials"][0]["status"] == ("unavailable" if reason else "failed"), (
        "HARDWARE_EXHAUSTION_UNAVAILABLE_OR_VALID_FAILURE_NOT_REPLACED"
    )
    assert not result["faults"]["passed"], "HARDWARE_UNAVAILABLE_FAILS_GATE"


def test_calibration_accumulates_media_counts_and_does_not_use_before_stop(tmp_path):
    hardware = hardware_module().Hardware(arguments(tmp_path))
    row = hardware_row()
    row.update(
        stage="speech",
        audio_started_monotonic=10,
        fault_stopped_monotonic=12.6,
        playback_launch_utc_ns=10_000_000_000,
        events=[{"stage": "stt-start", "t_utc_ns": 10_200_000_000}],
    )
    hardware.calibrate(row)
    expected = 2.6 - len(hardware.wake) / 32000 - (23680 + 24000) / 32000
    assert hardware.delay_samples == pytest.approx([expected]), (
        "CALIBRATION_FROM_MEDIA_POSITION"
    )
    row.update(
        stage="before",
        fault_stopped_monotonic=3,
        server_audio_samples=0,
        server_audio_last_chunk_samples=0,
    )
    hardware.calibrate(row)
    bootstrap = 0.2 + 23000 / 16000 - len(hardware.wake) / 32000
    assert hardware.delay_samples == pytest.approx([expected, bootstrap]), (
        "BEFORE_STOP_NOT_MEDIA_CLOCK"
    )
    assert statistics.median(hardware.delay_samples) == pytest.approx(
        (expected + bootstrap) / 2
    ), "ALL_DELAY_SAMPLES_ACCUMULATE"
