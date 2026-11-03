import asyncio
import copy
from contextlib import asynccontextmanager

import pytest

from tests.test_fault_runner import cleanup_bench as cleanup_bench
from tests.test_fault_runner import trial
from tools import fault_runner


@pytest.mark.parametrize(
    "outcome", ["success", "failure", "missing", "denied", "cancel", "delete-fails"]
)
async def test_fallback_baseline_uses_pipeline_entity_and_always_cleans_up(
    outcome, cleanup_bench, tmp_path, monkeypatch
):
    pipelines_before = copy.deepcopy(cleanup_bench["pipelines"])
    calls = []
    direct = []
    label = {"id": "clip", "audio_sha256": "a" * 64}
    engines = {"server": "stt.test", "fallback": "stt.fallback", "language": "en"}
    pcm = bytes(range(256)) * 5
    token_file = tmp_path / "authentication.json"
    token_file.write_text('{"token":"synthetic"}')

    @asynccontextmanager
    async def control(*_):
        yield cleanup_bench["command"]

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            return {"result": "success", "text": "direct text"}

    class Session:
        def post(self, *args, **kwargs):
            direct.append((args, kwargs))
            return Response()

    async def pipeline(*args, **kwargs):
        selected = next(p for p in cleanup_bench["pipelines"] if p["id"] == args[2])
        calls.append(copy.deepcopy(selected))
        assert args[3:5] == (label, pcm), "BASELINE_USES_SAME_PCM"
        assert kwargs["device_id"] == "virtual", "BASELINE_USES_TRIAL_DEVICE"
        row = trial()
        row["fallback_turns"][0]["destinations"] = []
        row.update(failures=[], recognized_transcript="pipeline text")
        if selected["stt_engine"] == "stt.fallback":
            assert args[5] == "fallback-baseline", "FALLBACK_BASELINE_HAS_NO_FAULT"
            assert kwargs.get("turn_sensor") is None, "NO_STALE_ASSIST_LIVE_TURN_SENSOR"
            if outcome == "cancel":
                raise asyncio.CancelledError
            if outcome == "failure":
                row["failures"] = ["stt-stream-failed"]
            if outcome == "missing":
                row["recognized_transcript"] = None
            if outcome == "denied":
                raise ValueError("pipeline run refused")
        return row

    monkeypatch.setattr(fault_runner, "control_command", control)
    monkeypatch.setattr(fault_runner, "pipeline_trial", pipeline)

    async def delete():
        raise ValueError("synthetic delete failure")

    if outcome == "delete-fails":
        cleanup_bench["delete"] = delete

    async def collect():
        return await fault_runner.collect_baselines(
            Session(),
            token_file,
            "existing",
            label,
            pcm,
            engines,
            None,
            device_id="virtual",
            turn_sensor="sensor.turn",
        )

    if outcome == "delete-fails":
        with pytest.raises(fault_runner.TemporaryPipelineError, match="DELETE_FAILED"):
            await collect()
        assert cleanup_bench["delete_attempts"] == 2, (
            "BASELINE_CLEANUP_FAILURE_IS_FATAL"
        )
        return
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await collect()
    else:
        baselines = await collect()
        assert [p["stt_engine"] for p in calls] == ["stt.test", "stt.fallback"], (
            "FALLBACK_BASELINE_USES_PIPELINE"
        )
        assert direct == [], "NO_DIRECT_STT_BASELINE"
        record = baselines["fallback"]
        assert record["interface"] == "assist_pipeline/run"
        assert record["pipeline_id"] == "temporary"
        assert record["pipeline_cleanup"]["bench_restored"] is True
        assert record["status"] == (
            "available" if outcome == "success" else "unavailable"
        )
        assert baselines["server"]["pipeline_id"] == "existing"
    assert cleanup_bench["pipelines"] == pipelines_before, "BASELINE_PIPELINE_RESTORED"
    assert cleanup_bench["delete_attempts"] == 1, (
        "BASELINE_PIPELINE_DELETED_ON_EVERY_PATH"
    )
    if calls:
        assert {
            k: v for k, v in calls[-1].items() if k not in ("id", "name", "stt_engine")
        } == {
            k: v
            for k, v in pipelines_before[0].items()
            if k not in ("id", "name", "stt_engine")
        }, "BASELINE_CHANGES_ONLY_STT_ENTITY"


@pytest.mark.parametrize("stage", ["baseline", "fallback-baseline", "after-final"])
async def test_pipeline_sender_uses_identical_pcm_frames_and_audio_settings(
    stage, tmp_path, monkeypatch
):
    from collections import deque

    sent = []
    requests = []
    finished = asyncio.Event()
    source = trial()
    pcm = bytes(range(256)) * 5
    original_sleep = asyncio.sleep

    async def sleep(_):
        await original_sleep(0)

    class Socket:
        def __init__(self):
            self.messages = deque()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def send_json(self, message):
            if message["id"] == 3:
                requests.append(message)
                self.messages.append(
                    {
                        "id": 3,
                        "type": "event",
                        "event": {
                            "type": "run-start",
                            "data": {"runner_data": {"stt_binary_handler_id": 7}},
                        },
                    }
                )
            else:
                self.messages.append(
                    {
                        "id": message["id"],
                        "type": "result",
                        "success": True,
                        "result": [],
                    }
                )

        async def send_bytes(self, data):
            sent.append(data)
            if data == b"\x07":
                finished.set()

        async def receive_json(self):
            if not self.messages:
                await asyncio.wait_for(finished.wait(), 1)
                self.messages.extend(
                    [
                        {
                            "id": 3,
                            "type": "event",
                            "event": {
                                "type": "stt-end",
                                "data": {"stt_output": {"text": "pipeline text"}},
                            },
                        },
                        {
                            "id": 2,
                            "type": "event",
                            "event": {
                                "event_type": "assist_live_test_record",
                                "data": {
                                    "recognized_transcript": "pipeline text",
                                    "device_id": "virtual",
                                    "turn_id": None
                                    if stage == "fallback-baseline"
                                    else "turn",
                                    "selected_intent": source["evaluated_intent"],
                                    "completed": True,
                                    "operations": [],
                                    "reply": "done",
                                },
                            },
                        },
                        {"id": 3, "type": "event", "event": {"type": "run-end"}},
                    ]
                )
            return self.messages.popleft()

    class Server:
        async def stop(self):
            assert stage == "after-final", "BASELINES_NEVER_INJECT_FAULT"

        async def restore(self):
            pass

    async def connect(*_):
        return Socket()

    async def snapshot(*_):
        return {
            "fallback_reasons": {},
            "turns": [{"turn_id": "turn", "destinations": []}],
        }

    monkeypatch.setattr(fault_runner, "connect", connect)
    monkeypatch.setattr(fault_runner, "fallback_snapshot", snapshot)
    monkeypatch.setattr(fault_runner.asyncio, "sleep", sleep)
    baselines = {
        engine: {
            "interface": "assist_pipeline/run",
            "pipeline_id": engine,
            "engine_id": "stt." + engine,
        }
        for engine in ("server", "fallback")
    }
    row = await fault_runner.pipeline_trial(
        None,
        tmp_path / "unused",
        "chosen",
        {"id": "clip", "audio_sha256": "a" * 64, "split": "held_out"},
        pcm,
        stage,
        Server(),
        device_id="virtual",
        baselines=baselines,
    )
    assert requests == [
        {
            "id": 3,
            "type": "assist_pipeline/run",
            "pipeline": "chosen",
            "start_stage": "stt",
            "end_stage": "intent",
            "input": {"sample_rate": 16000},
            "device_id": "virtual",
        }
    ], "BASELINE_AND_TRIAL_AUDIO_SETTINGS_IDENTICAL"
    assert sent == [b"\x07" + pcm[:640], b"\x07" + pcm[640:]] + [
        b"\x07" + bytes(640)
    ] * 100 + [b"\x07"], "BASELINE_AND_TRIAL_PCM_FRAMES_IDENTICAL"
    assert row["baseline_paths"] == baselines, "TRIAL_NAMES_EACH_BASELINE_PATH"
    if stage in ("baseline", "fallback-baseline"):
        assert row["failures"] == [], "BASELINE_NO_ASSIST_LIVE_DIAGNOSTICS_REQUIRED"
