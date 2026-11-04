import asyncio
import copy
import json
from argparse import Namespace
from contextlib import asynccontextmanager

import pytest

from tests.test_fault_hardware import arguments, cancel_fakes, hardware_row
from tests.test_fault_runner import set_reasons, trial
from tools import fault_hardware, fault_runner


def cancel_response(count=0):
    return {
        "response": {
            "response_type": "action_done",
            "speech": {},
            "card": {},
            "language": "en",
            "speech_slots": {"canceled": count},
            "data": {"success": [], "failed": []},
        },
        "conversation_id": "synthetic",
        "continue_conversation": False,
    }


@pytest.mark.parametrize("count", [0, 2])
async def test_timer_cancel_sends_exact_native_request(tmp_path, count):
    hardware = fault_hardware.Hardware(arguments(tmp_path))
    hardware.device_id = "physical"
    calls = []

    async def command(kind, **fields):
        calls.append((kind, fields))
        return cancel_response(count)

    await hardware.cancel_timers(command)
    assert calls == [
        (
            "conversation/process",
            {
                "text": "cancel all timers",
                "language": "en",
                "agent_id": "conversation.home_assistant",
                "device_id": "physical",
            },
        )
    ], "EXACT_NATIVE_TIMER_CANCEL"
    assert hardware.timer_cancel_calls == [{"canceled": count}], "CANCEL_COUNT_RETAINED"


async def test_timer_cancel_missing_count_is_null(tmp_path):
    hardware = fault_hardware.Hardware(arguments(tmp_path))

    async def command(*_, **__):
        return {"response": {"response_type": "action_done"}}

    await hardware.cancel_timers(command)
    assert hardware.timer_cancel_calls == [{"canceled": None}], (
        "MISSING_CANCEL_COUNT_IS_NULL"
    )


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"response": None},
        {"response": {}},
        {"response": {"response_type": "error", "data": {"code": "unknown"}}},
    ],
)
async def test_timer_cancel_rejects_error_or_missing_response(tmp_path, response):
    hardware = fault_hardware.Hardware(arguments(tmp_path))
    hardware.device_id = "physical"

    async def command(*_, **__):
        return response

    with pytest.raises(ValueError, match="timer_cancel_failed"):
        await hardware.cancel_timers(command)


async def test_timer_cancel_names_transport_failure(tmp_path):
    hardware = fault_hardware.Hardware(arguments(tmp_path))
    hardware.device_id = "physical"

    async def command(*_, **__):
        raise OSError("synthetic transport error")

    with pytest.raises(ValueError, match="timer_cancel_failed") as error:
        await hardware.cancel_timers(command)
    assert isinstance(error.value.__cause__, OSError), "CANCEL_TRANSPORT_CAUSE_RETAINED"


async def test_real_operation_capture_closes_before_cancelled_event(
    tmp_path, monkeypatch
):
    state = cancel_fakes(tmp_path, monkeypatch)
    state["trial_finished"] = True
    original_connect = fault_runner.connect
    subscribers = set()
    expected = copy.deepcopy(trial()["action_trace"])
    record = {
        "turn_id": "turn-new",
        "device_id": "physical",
        "completed": True,
        "recognized_transcript": "test transcript",
        "selected_intent": trial()["evaluated_intent"],
        "operations": [
            {
                "service": operation["service"],
                "target": operation["virtual_target"],
                "arguments": operation["arguments"],
                "t_monotonic_ns": operation["monotonic_ns"],
            }
            for operation in expected
        ],
    }
    cancelled_events = []

    class Socket:
        def __init__(self, socket):
            self.socket = socket
            self.sent_record = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            subscribers.discard(self)

        async def send_json(self, message):
            if message.get("type") == "subscribe_events":
                subscribers.add(self)
            await self.socket.send_json(message)

        async def receive_json(self):
            if self.socket.mode == "events" and not self.socket.first_event:
                if not self.sent_record:
                    await state["playback_started"].wait()
                    self.sent_record = True
                    return {
                        "event": {
                            "event_type": "assist_live_test_record",
                            "data": record,
                        }
                    }
                await asyncio.Event().wait()
            return await self.socket.receive_json()

    async def connect(*args):
        return Socket(await original_connect(*args))

    async def command(kind, **fields):
        if kind == "conversation/process":
            cancelled_events.append(
                {"event": "cancelled", "device_id": fields["device_id"]}
            )
            if subscribers:
                record["operations"].append(
                    {
                        "service": "timer.cancelled",
                        "target": "physical",
                        "arguments": {},
                        "t_monotonic_ns": 123,
                    }
                )
            return cancel_response(1)
        return await state["command"](kind, **fields)

    @asynccontextmanager
    async def control(*_):
        yield command

    monkeypatch.setattr(fault_runner, "connect", connect)
    monkeypatch.setattr(fault_runner, "control_command", control)
    hardware = fault_hardware.Hardware(arguments(tmp_path))
    await hardware.preflight(command)
    async with hardware.selected(command, "temporary", {}):
        row = await hardware.trial(
            None,
            state["token"],
            "temporary",
            state["label"],
            state["pcm"],
            "qualification",
            state["server"](),
            sensor="sensor.turn",
            stop_latency_seconds=0.1,
            stop_margin_seconds=0.02,
        )
        await hardware.cancel_timers(command)
    assert row["action_trace_complete"] is True, "REAL_OPERATION_CAPTURE_COMPLETED"
    assert row["action_trace"] == expected, (
        "REAL_CANCELLED_EVENT_OUTSIDE_OPERATION_CAPTURE"
    )
    assert row["agent_records"][0]["operations"] == record["operations"]
    assert all(op["service"] != "timer.cancelled" for op in record["operations"]), (
        "REAL_CANCELLED_EVENT_OUTSIDE_AGENT_RECORD"
    )
    assert len(cancelled_events) == 2, "REAL_CAPTURE_CANCELLED_EVENTS_EMITTED"


@pytest.fixture
def timer_run(tmp_path, monkeypatch):
    state = cancel_fakes(tmp_path, monkeypatch)
    timeline = []
    operations = []
    state.update(
        timeline=timeline,
        cancels=0,
        fail_cancels=set(),
        stop=None,
        timer_events=[],
        select_error=False,
    )
    label = {
        **trial()["label"],
        "id": "test",
        "split": "held_out",
        "audio_sha256": "a" * 64,
        "alignment_words": [{"start_sample": 16000, "end_sample": 48000}],
    }

    async def command(kind, **fields):
        if kind != "conversation/process":
            value = await state["command"](kind, **fields)
            if (
                state["select_error"]
                and kind == "call_service"
                and fields["service_data"]["option"] == "Temporary"
            ):
                raise RuntimeError("selection failed after apply")
            return value
        state["cancels"] += 1
        timeline.append("cancel")
        state["timer_events"].append({"event": "cancelled", "device_id": "physical"})
        operations.append({"service": "timer.cancelled"})
        if state["cancels"] in state["fail_cancels"]:
            return {"response": {"response_type": "error"}}
        return cancel_response(1)

    @asynccontextmanager
    async def control(*_):
        yield command

    async def play(self, *args, **kwargs):
        stage = args[5]
        timeline.extend(["idle:" + stage, "play:" + stage])
        if state["stop"] is not None:
            raise state["stop"]
        operations.clear()
        row = hardware_row()
        row.update(
            label=label,
            clip_id="test",
            stage=stage,
            failures=[],
            failure_messages=[],
        )
        operations.extend(copy.deepcopy(row["action_trace"]))
        if stage not in ("qualification", "baseline"):
            set_reasons(row, stage)
        return row

    async def baselines(self, *args):
        row = args[7]
        timeline.append("capture:" + row["stage"])
        row["action_trace"] = copy.deepcopy(operations)
        for engine, baseline in row["baselines"].items():
            baseline["evidence"] = trial()
            baseline["engine_id"] = "stt.test" if engine == "server" else "stt.fallback"
        timeline.append("baselines:" + row["stage"])

    monkeypatch.setattr(fault_runner, "control_command", control)
    monkeypatch.setattr(fault_runner, "load_corpus", lambda _: [label])
    monkeypatch.setattr(fault_runner, "audio_input", lambda *_: b"synthetic")
    monkeypatch.setattr(fault_hardware.Hardware, "trial", play)
    monkeypatch.setattr(fault_hardware.Hardware, "baselines", baselines)

    async def run(*, qualify_only=False, stage="before"):
        return await fault_runner.run(
            Namespace(
                **vars(arguments(tmp_path)),
                output=tmp_path / "result.json",
                simulate=False,
                check=True,
                labels=tmp_path / "labels.jsonl",
                native_matrix=None,
                stages=stage,
                each=2,
                audio_root=tmp_path,
                token_file=state["token"],
                temporary_agent_pipeline=True,
                pipeline="source",
                qualify_only=qualify_only,
            )
        )

    state["run"] = run
    state["output"] = tmp_path / "result.json"
    return state


async def test_timers_removed_before_play_after_capture_and_in_cleanup(timer_run):
    result = await timer_run["run"]()
    assert result["timer_cancel_calls"] == [{"canceled": 1}] * 5, (
        "EVERY_CANCEL_COUNT_IN_PRIVATE_RESULT"
    )
    assert timer_run["timeline"] == [
        "cancel",
        "idle:qualification",
        "play:qualification",
        "capture:qualification",
        "baselines:qualification",
        "cancel",
        "idle:before",
        "play:before",
        "capture:before",
        "baselines:before",
        "cancel",
        "idle:before",
        "play:before",
        "capture:before",
        "baselines:before",
        "cancel",
        "cancel",
    ], "CANCEL_AFTER_EACH_CAPTURE_BEFORE_NEXT_IDLE_AND_CLEANUP"
    assert len(result["attempts"]) == 2
    assert all(row["status"] == "passed" for row in result["attempts"]), (
        "CANCELLED_EVENT_OUTSIDE_GRADING"
    )
    assert all(
        operation["service"] != "timer.cancelled"
        for row in result["attempts"] + result["qualification"]["recordings"]
        for operation in row["action_trace"]
    ), "CANCELLED_EVENT_OUTSIDE_OPERATION_CAPTURE"


async def test_qualification_only_also_removes_timers(timer_run):
    await timer_run["run"](qualify_only=True)
    assert timer_run["timeline"] == [
        "cancel",
        "idle:qualification",
        "play:qualification",
        "capture:qualification",
        "baselines:qualification",
        "cancel",
        "cancel",
    ], "QUALIFICATION_ONLY_TIMER_CLEANUP"


async def test_missing_live_turn_link_keeps_outer_cleanup(timer_run, monkeypatch):
    play = fault_hardware.Hardware.trial

    async def unlinked(self, *args, **kwargs):
        row = await play(self, *args, **kwargs)
        row["agent_records"] = [
            {
                "turn_id": None,
                "device_id": "physical",
                "completed": True,
                "recognized_transcript": row["recognized_transcript"],
            }
        ]
        return row

    monkeypatch.setattr(fault_hardware.Hardware, "trial", unlinked)
    result = await timer_run["run"]()
    assert timer_run["timeline"] == [
        "cancel",
        "idle:qualification",
        "play:qualification",
        "cancel",
    ], "MISSING_AGENT_LINK_CLEANUP_WITHOUT_NEXT_PLAY"
    assert result["agent_turn_link_missing"] is True
    assert result["bench_restored"] and result["restored_healthy"], (
        "MISSING_AGENT_LINK_RESTORES_BENCH"
    )
    assert result["timer_cancel_calls"] == [{"canceled": 1}, {"canceled": 1}]


async def test_playback_calibration_also_removes_timers_after_capture(timer_run):
    await timer_run["run"](stage="speech")
    timeline = timer_run["timeline"]
    start = timeline.index("play:baseline")
    assert timeline[start - 1 : start + 3] == [
        "idle:baseline",
        "play:baseline",
        "cancel",
        "idle:speech",
    ], "CALIBRATION_TIMER_CANCEL_BEFORE_NEXT_IDLE"


async def test_cancelled_timer_event_does_not_enter_operations_or_grading(timer_run):
    result = await timer_run["run"]()
    assert (
        timer_run["timer_events"]
        == [{"event": "cancelled", "device_id": "physical"}] * 5
    ), "CANCEL_EMITS_TIMER_CANCELLED_EVENTS"
    for row in result["attempts"] + result["qualification"]["recordings"]:
        assert row["action_trace"] == trial()["action_trace"], (
            "CANCELLED_EVENT_OUTSIDE_OPERATION_CAPTURE"
        )
    assert [row["status"] for row in result["attempts"]] == ["passed", "passed"], (
        "CANCELLED_EVENT_OUTSIDE_GRADING"
    )


async def test_cancel_before_run_failure_starts_no_play(timer_run):
    timer_run["fail_cancels"] = {1}
    result = await timer_run["run"]()
    assert timer_run["timeline"] == ["cancel", "cancel"], "FAILED_CANCEL_NO_PLAY"
    assert result.get("timer_cancel_failed") is True, "PRE_RUN_CANCEL_FAILURE_NAMED"
    assert result["faults"]["reason"] == "timer_cancel_failed"
    assert result["attempts"] == []
    assert result["bench_restored"] and result["restored_healthy"]


@pytest.mark.parametrize("failed", [2, 3])
async def test_cancel_after_attempt_failure_keeps_result_and_stops(timer_run, failed):
    timer_run["fail_cancels"] = {failed}
    result = await timer_run["run"]()
    plays = [item for item in timer_run["timeline"] if item.startswith("play:")]
    assert plays == ["play:qualification", "play:before"][: failed - 1], (
        "FAILED_POST_ATTEMPT_CANCEL_STOPS_NEXT_PLAY"
    )
    assert timer_run["cancels"] == failed + 1, "CLEANUP_RETRIES_TIMER_CANCEL"
    assert result.get("timer_cancel_failed") is True, (
        "POST_ATTEMPT_CANCEL_FAILURE_NAMED"
    )
    assert result["faults"]["reason"] == "timer_cancel_failed"
    assert result["qualification"]["recordings"][0]["eligible"] is True
    if failed == 3:
        assert result["attempts"][0]["status"] == "passed", "ATTEMPT_STATUS_PRESERVED"
    assert result["bench_restored"] and result["restored_healthy"]


@pytest.mark.parametrize("stop", [asyncio.CancelledError(), RuntimeError("original")])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_cleanup_cancel_on_stop_never_masks_original(
    timer_run, stop, cleanup_fails
):
    timer_run["stop"] = stop
    if cleanup_fails:
        timer_run["fail_cancels"] = {2}
    with pytest.raises(
        type(stop), match="original" if isinstance(stop, RuntimeError) else None
    ):
        await timer_run["run"]()
    assert timer_run["timeline"] == [
        "cancel",
        "idle:qualification",
        "play:qualification",
        "cancel",
    ], "CLEANUP_CANCEL_ON_ERROR_AND_CANCELLATION"
    written = json.loads(timer_run["output"].read_text())
    assert written["bench_restored"] and written["restored_healthy"]
    if cleanup_fails:
        assert written.get("timer_cancel_failed") is True, (
            "CLEANUP_CANCEL_FAILURE_NAMED"
        )


async def test_cleanup_cancel_failure_keeps_sufficient_qualification_but_fails_gate(
    timer_run,
):
    timer_run["fail_cancels"] = {3}
    result = await timer_run["run"](qualify_only=True)
    assert result.get("timer_cancel_failed") is True, "CLEANUP_FAILURE_STOPS_RUN"
    assert result["qualification"]["sufficient"] is True
    assert result["faults"]["passed"] is False
    assert result["faults"]["reason"] == "timer_cancel_failed"


async def test_cleanup_cancel_runs_after_selection_error_and_keeps_original(timer_run):
    timer_run["select_error"] = True
    timer_run["fail_cancels"] = {2}
    with pytest.raises(RuntimeError, match="selection failed after apply"):
        await timer_run["run"]()
    assert timer_run["timeline"] == ["cancel", "cancel"], (
        "CANCEL_AFTER_EARLIER_SETUP_ERROR"
    )
    written = json.loads(timer_run["output"].read_text())
    assert written["timer_cancel_failed"] is True
    assert written["satellite_pipeline_restored"] and written["bench_restored"]


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_outer_cleanup_cancels_after_ready_error(
    timer_run, monkeypatch, cleanup_fails
):
    original_ready = fault_hardware.Hardware.ready
    checks = 0

    async def ready(self, command):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("ready failed after preflight")
        return await original_ready(self, command)

    monkeypatch.setattr(fault_hardware.Hardware, "ready", ready)
    if cleanup_fails:
        timer_run["fail_cancels"] = {1}
    with pytest.raises(RuntimeError, match="ready failed after preflight") as error:
        await timer_run["run"]()
    assert timer_run["timeline"] == ["cancel"], "OUTER_CANCEL_AFTER_READY_ERROR"
    if cleanup_fails:
        assert "timer_cancel_failed" in error.value.__notes__, (
            "CANCEL_FAILURE_IS_ERROR_NOTE"
        )
    written = json.loads(timer_run["output"].read_text())
    assert written["bench_restored"] and written["restored_healthy"]


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_outer_cleanup_cancels_after_initial_server_restore_error(
    timer_run, monkeypatch, cleanup_fails
):
    original_restore = fault_runner.TestServer.restore
    restores = 0

    async def restore(self):
        nonlocal restores
        restores += 1
        if restores == 1:
            raise RuntimeError("initial server restore failed")
        await original_restore(self)

    monkeypatch.setattr(fault_runner.TestServer, "restore", restore)
    if cleanup_fails:
        timer_run["fail_cancels"] = {1}
    with pytest.raises(RuntimeError, match="initial server restore failed") as error:
        await timer_run["run"]()
    assert timer_run["timeline"] == ["cancel"], (
        "OUTER_CANCEL_AFTER_SERVER_RESTORE_ERROR"
    )
    assert restores == 2, "SERVER_RESTORE_RETRIED_AFTER_TIMER_CLEANUP"
    if cleanup_fails:
        assert "timer_cancel_failed" in error.value.__notes__, (
            "CANCEL_FAILURE_IS_ERROR_NOTE"
        )


def test_qualification_cli_refuses_timer_cancel_failure(monkeypatch, tmp_path):
    async def run(_):
        return {"qualification": {"sufficient": True}, "timer_cancel_failed": True}

    monkeypatch.setattr(fault_runner, "run", run)
    status = fault_runner.main(
        [
            "--hardware",
            "--qualify-only",
            "--satellite",
            "assist_satellite.test",
            "--playback-sink",
            "test-sink",
            "--wake-audio",
            "wake.wav",
            "--ha-container",
            "test-ha",
            "--turn-audio-archive",
            "archive",
            "--token-file",
            "authentication.json",
            "--pipeline",
            "test",
            "--audio-root",
            "audio",
            "--temporary-agent-pipeline",
            "--output",
            str(tmp_path / "result.json"),
        ]
    )
    assert status == 1, "QUALIFICATION_TIMER_CANCEL_FAILURE_FAILS_CLI"
