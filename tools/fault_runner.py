import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
import wave
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.corpus import load_corpus, resolve_audio
from tools.device_replay import BRIDGE_OPERATION_MAP, RUN_FIELDS
from tools.measurement import LOSS_FLAGS, capture_loss, loss_window
from tools.metrics import rate
from tools.native_matrix import (
    main as native_main,
)
from tools.native_matrix import (
    run_native_matrix,
    run_virtual,
    validate_matrix_document,
    validate_native_matrix,
)
from tools.probe_pipelines import Command, pipeline_fields
from tools.result_text import words
from tools.test_environment import FIXTURES, URL, expected_operations, intent_matches

Row = dict[str, Any]
STAGES = ("before", "speech", "pause", "after-final")
CONTAINER = "assist-live-server-q1"
ROOT = Path(__file__).resolve().parents[1]
TEMPORARY_PIPELINE_NAME = "assist-live f11-smoke"
FALLBACK_BASELINE_PIPELINE_NAME = "assist-live fallback baseline"
STOP_CALIBRATION_SAMPLES = 5
MAX_SLOT_ATTEMPTS = 3
PIPELINE_CLEANUP_TIMEOUT = 30.0
FALLBACK_LIMIT_NS = 20_000_000_000
EXPECTED_REASONS = {
    "before": {"health_timeout", "health_error", "server_unhealthy", "open_error"},
    "speech": {"stream_closed", "message_timeout", "stream_error"},
    "pause": {"stream_closed", "message_timeout", "stream_error"},
    "after-final": set(),
}


class TemporaryPipelineError(ValueError):
    pass


def no_text_error(trial: Row) -> bool:
    run_id = trial.get("pipeline_request_id")
    events = trial.get("events")
    if (
        type(run_id) is not int
        or run_id <= 0
        or not isinstance(events, list)
        or trial.get("recognized_transcript") not in (None, "")
        or trial.get("evaluated_intent") is not None
        or trial.get("agent_records") != []
        or not all(
            isinstance(event, dict)
            and event.get("request_id") == run_id
            and event.get("source") == "ha"
            for event in events
        )
    ):
        return False
    stages = [event.get("stage") for event in events]
    errors = [event for event in events if event.get("stage") == "error"]
    return (
        stages[:2] == ["run-start", "stt-start"]
        and stages[-2:] == ["error", "run-end"]
        and not any(
            stage in ("stt-end", "intent-start", "intent-end") for stage in stages
        )
        and len(errors) == 1
        and isinstance(errors[0].get("data"), dict)
        and errors[0].get("data", {}).get("code") == "stt-no-text-recognized"
    )


def reason_delta(before: object, after: object) -> dict[str, int] | None:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    if any(
        not isinstance(key, str) or type(value) is not int or value < 0
        for counts in (before, after)
        for key, value in counts.items()
    ):
        return None
    delta = {key: after.get(key, 0) - before.get(key, 0) for key in before | after}
    if any(value < 0 for value in delta.values()):
        return None
    return {key: value for key, value in delta.items() if value}


def stt_reason_for_turn(trial: Row) -> tuple[bool, str | None]:
    turns = trial.get("fallback_turns")
    turn_id = trial.get("turn_id")
    if not isinstance(turns, list) or not isinstance(turn_id, str) or not turn_id:
        return False, None
    records = [
        record
        for record in turns
        if isinstance(record, dict) and record.get("turn_id") == turn_id
    ]
    if len(records) != 1:
        return False, None
    destinations = records[0].get("destinations")
    if not isinstance(destinations, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("part"), str)
        for row in destinations
    ):
        return False, None
    fallbacks = [row for row in destinations if row["part"] == "stt_fallback"]
    if not fallbacks:
        return True, None
    if len(fallbacks) != 1:
        return False, None
    reason = fallbacks[0].get("fallback_reason")
    return (True, reason) if isinstance(reason, str) and reason else (False, None)


def recorded_reason_matches(trial: Row) -> bool:
    if trial.get("scope") == "hardware":
        from tools.fault_hardware import recorded_reason_matches as hardware_reasons

        return hardware_reasons(trial)
    delta = reason_delta(
        trial.get("fallback_reasons_before"), trial.get("fallback_reasons_after")
    )
    stage = trial.get("stage")
    if delta is None or delta != trial.get("fallback_reasons_delta"):
        return False
    evidence, reason = stt_reason_for_turn(trial)
    if (
        not evidence
        or reason != trial.get("failure_cause")
        or delta != ({reason: 1} if reason is not None else {})
    ):
        return False
    if stage in ("after-final", "baseline"):
        return delta == {} and trial.get("failure_cause") is None
    return (
        stage in EXPECTED_REASONS
        and sum(delta.values()) == 1
        and set(delta) <= EXPECTED_REASONS[stage]
        and trial.get("failure_cause") == next(iter(delta), None)
    )


def transcript_engine(trial: Row) -> str | None:
    evidence, reason = stt_reason_for_turn(trial)
    return ("fallback" if reason else "server") if evidence else None


def selected_baseline(trial: Row) -> Row | None:
    engine = transcript_engine(trial)
    baselines = trial.get("baselines")
    engines = trial.get("stt_engines")
    if not isinstance(baselines, dict) or not isinstance(engines, dict):
        return None
    baseline = baselines.get(engine)
    if not isinstance(baseline, dict) or not (
        baseline.get("status") == "available"
        and isinstance(baseline.get("engine_id"), str)
        and baseline["engine_id"]
        and baseline["engine_id"] == engines.get(engine)
        and baseline.get("clip_id") == trial.get("clip_id")
        and baseline.get("audio_sha256") == trial.get("audio_sha256")
        and isinstance(baseline.get("transcript"), str)
        and baseline["transcript"].strip()
    ):
        return None
    return baseline


def injection_status(trial: Row) -> str:
    if trial.get("scope") == "hardware":
        from tools.fault_hardware import injection_status as hardware_injection_status

        return hardware_injection_status(trial)
    if trial.get("stage") not in ("speech", "pause"):
        return "verified"
    try:
        window = fault_window(trial.get("label", {}), trial["stage"])
    except ValueError:
        return "unavailable"
    position = trial.get("fault_stopped_audio_sample")
    if window is None or type(position) is not int:
        return "unavailable"
    return "verified" if window[0] <= position < window[1] else "invalid_injection"


def trial_status(trial: Row) -> str:
    injection = injection_status(trial)
    if injection != "verified":
        return injection
    if selected_baseline(trial) is None:
        return "unavailable"
    return "passed" if acceptable(trial) else "failed"


def acceptable(trial: Row) -> bool:
    if trial.get("scope") == "hardware":
        events = trial.get("events", [])
        if (
            not isinstance(events, list)
            or any(not isinstance(event, dict) for event in events)
            or sum(event.get("stage") == "run-end" for event in events) != 1
            or any(event.get("stage") == "error" for event in events)
        ):
            return False
    if injection_status(trial) != "verified":
        return False
    baseline = selected_baseline(trial)
    if baseline is None:
        return False
    if not all(
        key in trial
        for key in ("recognized_transcript", "evaluated_intent", "action_trace")
    ):
        return False
    transcript = trial.get("recognized_transcript")
    selected = trial.get("evaluated_intent")
    trace = trial.get("action_trace")
    label = trial.get("label")
    deadline = trial.get("fallback_deadline_utc_ns")
    completed = trial.get("completed_utc_ns")
    starts = [
        event.get("t_utc_ns")
        for event in trial.get("events", [])
        if isinstance(event, dict) and event.get("stage") == "run-start"
    ]
    if not (
        isinstance(trace, list)
        and all(
            isinstance(operation, dict)
            and type(operation.get("monotonic_ns")) is int
            and operation["monotonic_ns"] > 0
            for operation in trace
        )
        and trial.get("action_trace_complete") is True
        and isinstance(trial.get("turn_id"), str)
        and trial["turn_id"]
        and isinstance(label, dict)
        and isinstance(label.get("intent"), dict)
        and trial.get("stage") in STAGES
        and trial.get("fault_injected") is True
        and trial.get("injected_fault") == "server_container_stopped"
        and recorded_reason_matches(trial)
        and len(starts) == 1
        and type(starts[0]) is int
        and type(deadline) is int
        and deadline == starts[0] + FALLBACK_LIMIT_NS
        and type(completed) is int
        and 0 < completed <= deadline
    ):
        return False
    if no_text_error(trial):
        return trace == [] and trial.get("operations") == []
    if not (
        isinstance(transcript, str)
        and bool(transcript.strip())
        and isinstance(selected, dict)
        and isinstance(selected.get("name"), str)
        and selected["name"]
        and isinstance(selected.get("slots"), dict)
    ):
        return False
    if words(transcript) != words(baseline["transcript"]):
        return False
    inventory = json.loads((FIXTURES / "intents.json").read_text())
    try:
        expected = expected_operations(label, inventory)
        actual = [
            {
                "service": operation["service"],
                "target": operation["virtual_target"],
                "arguments": operation["arguments"],
            }
            for operation in trace
        ]
        return (
            expected is not None
            and intent_matches(selected, label["intent"])
            and Counter(json.dumps(item, sort_keys=True) for item in actual)
            == Counter(json.dumps(item, sort_keys=True) for item in expected)
            and (
                "operations" not in trial
                or Counter(
                    json.dumps(
                        {key: item[key] for key in ("service", "target", "arguments")},
                        sort_keys=True,
                    )
                    for item in trial["operations"]
                )
                == Counter(json.dumps(item, sort_keys=True) for item in actual)
            )
        )
    except KeyError, TypeError, ValueError:
        return False


def fault_gate(faults: Row) -> Row:
    slots = faults.get("trials", [])
    statuses = [(row, trial_status(row)) for row in slots]
    trials = [row for row, status in statuses if status in ("passed", "failed")]
    outcomes = Counter(
        (row.get("stage"), status)
        for row, status in statuses
        if status in ("passed", "failed")
    )
    passed = sum(count for (_, status), count in outcomes.items() if status == "passed")
    counts = Counter(trial.get("stage") for trial in trials)
    summary = {
        **rate(passed, len(trials)),
        "stages": {
            stage: {
                "counted_trials": counts[stage],
                "passed": outcomes[(stage, "passed")],
                "failed": outcomes[(stage, "failed")],
                "unavailable": sum(row.get("stage") == stage for row in slots)
                - counts[stage],
                "invalid_attempts": sum(
                    row.get("stage") == stage
                    and injection_status(row) == "invalid_injection"
                    for row in faults.get("attempts", [])
                ),
            }
            for stage in STAGES
        },
        "passed": len(slots) == len(trials) == 40
        and len({trial.get("turn_id") for trial in trials}) == 40
        and passed == 40
        and counts == {stage: 10 for stage in STAGES}
        and not any(trial.get("scope") == "simulated" for trial in trials),
    }
    qualification = faults.get("qualification")
    if qualification is not None:
        summary["qualification"] = {
            "candidates": qualification.get("candidates", 0),
            "eligible": qualification.get("eligible", 0),
            "needed": qualification.get("target", 0),
            "causes": dict(
                Counter(
                    cause
                    for row in qualification.get("recordings", [])
                    if (cause := row.get("cause")) is not None
                )
            ),
        }
        if qualification.get("sufficient") is not True:
            summary.update(passed=False, reason="insufficient_eligible_recordings")
    return summary


def audio_input(label: Row, root: Path) -> bytes:
    path = resolve_audio(root, label["audio_path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != label["audio_sha256"]:
        raise ValueError("audio SHA-256 mismatch")
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (
            1,
            2,
            16000,
        ):
            raise ValueError("audio requires mono PCM16 at 16000 Hz")
        return wav.readframes(wav.getnframes())


def fault_window(label: Row, stage: str) -> tuple[int, int] | None:
    if stage == "speech":
        candidates = [
            word
            for word in label.get("alignment_words", [])
            if type(word.get("start_sample")) is int
            and type(word.get("end_sample")) is int
            and 0 <= word["start_sample"] < word["end_sample"]
        ]
        if not candidates:
            raise ValueError("label has no fault boundary: " + stage)
        word = max(
            candidates, key=lambda word: word["end_sample"] - word["start_sample"]
        )
        start, end = word.get("start_sample"), word.get("end_sample")
    elif stage == "pause":
        start, end = label.get("pause_start_sample"), label.get("pause_end_sample")
    else:
        return None
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise ValueError("label has no fault boundary: " + stage)
    return start, end


def fault_sample(
    label: Row,
    stage: str,
    *,
    stop_latency_seconds: float = 0,
    stop_margin_seconds: float = 0.02,
) -> int | None:
    window = fault_window(label, stage)
    if window and stage == "speech":
        if not math.isfinite(stop_latency_seconds) or stop_latency_seconds < 0:
            raise ValueError("invalid stop latency")
        if not math.isfinite(stop_margin_seconds) or stop_margin_seconds < 0.02:
            raise ValueError("invalid stop margin")
        latency = math.ceil(round(stop_latency_seconds * 16000, 9))
        margin = math.ceil(round(stop_margin_seconds * 16000, 9))
        if window[1] - window[0] < 2 * margin:
            raise ValueError("voice window too short for stop margin")
        request = sum(window) // 2 - latency
        if request < 0:
            raise ValueError("stop request before audio start")
        return request
    return (
        max(
            0,
            window[0]
            + (window[1] - window[0]) // 4
            - int(stop_latency_seconds * 16000),
        )
        if window
        else None
    )


class TestServer:
    async def docker(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(45):
                stdout, _ = await process.communicate()
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            raise ValueError("isolated server container command failed")
        return stdout.decode().strip()

    async def stop(self) -> None:
        await self.docker("stop", "--time", "0", CONTAINER)

    async def measure_stop_latency(self) -> float:
        started = time.monotonic()
        try:
            await self.stop()
            return time.monotonic() - started
        finally:
            await self.restore()

    async def restore(self) -> None:
        await self.docker("start", CONTAINER)
        for _ in range(60):
            state = json.loads(
                await self.docker("inspect", "--format", "{{json .State}}", CONTAINER)
            )
            if (
                state.get("Running") is True
                and state.get("Health", {}).get("Status") == "healthy"
            ):
                return
            await asyncio.sleep(1)
        raise ValueError("isolated server did not return to running and healthy")


async def connect(
    session: aiohttp.ClientSession, token_file: Path
) -> aiohttp.ClientWebSocketResponse:
    token = json.loads(token_file.read_text())["token"]
    if not isinstance(token, str) or not token:
        raise ValueError("test authentication unavailable")
    ws = await session.ws_connect(URL + "/api/websocket", heartbeat=20)
    try:
        async with asyncio.timeout(15):
            if (await ws.receive_json()).get("ha_version") != "2026.10.3":
                raise ValueError("isolated test instance requires HA 2026.10.3")
            await ws.send_json({"type": "auth", "access_token": token})
            if (await ws.receive_json()).get("type") != "auth_ok":
                raise ValueError("test authentication failed")
    except BaseException:
        await ws.close()
        raise
    return ws


@asynccontextmanager
async def control_command(
    session: aiohttp.ClientSession, token_file: Path
) -> AsyncIterator[Command]:
    async with await connect(session, token_file) as control:
        responses: asyncio.Queue[Any] = asyncio.Queue()
        counter = 0

        async def receive() -> None:
            try:
                while True:
                    await responses.put(await control.receive_json())
            except Exception as error:
                await responses.put(error)

        # aiohttp answers HA PING frames only while a receive call runs.
        reader = asyncio.create_task(receive())

        async def command(kind: str, **fields: Any) -> Any:
            nonlocal counter
            counter += 1
            async with asyncio.timeout(30):
                await control.send_json({"id": counter, "type": kind, **fields})
                while True:
                    response = await responses.get()
                    if isinstance(response, Exception):
                        raise response
                    if response.get("id") == counter:
                        break
            if response.get("success") is not True:
                message = str(response.get("error", {}).get("message", "unknown error"))
                raise ValueError("isolated bench control command failed: " + message)
            return response.get("result")

        try:
            yield command
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


def scrub_cleanup_error(error: Exception, redactions: tuple[str, ...] = ()) -> str:
    message = str(error)
    for value in redactions:
        if value:
            message = message.replace(value, "[token]")
    message = re.sub(r"(?i)\b(?:https?|wss?)://\S+", "[url]", message)
    message = re.sub(
        r"(?i)(?:bearer\s+|(?:access_token|token|authorization)\s*[=:]\s*)[^\s,;]+",
        "[token]",
        message,
    )
    message = re.sub(r"\b[A-Za-z0-9_.=-]{20,}\b", "[token]", message)
    message = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "[address]", message)
    message = re.sub(r"\b[\w.-]+\.[a-zA-Z]{2,}(?::\d+)?\b", "[address]", message)
    message = re.sub(r"[\da-fA-F]*:[\da-fA-F:]+(?::\d+)?", "[address]", message)
    return message[:1000]


def failure_message(error: BaseException) -> str:
    message = f"{type(error).__name__}: {scrub_cleanup_error(ValueError(str(error)))}"
    return message if len(message) <= 500 else message[:488] + " [truncated]"


def complete_failure_messages(row: Row) -> None:
    if not row.get("failure_messages"):
        causes = row.get("failures") or [
            row.get("unavailable_reason")
            or row.get("hardware_invalid_reason")
            or "trial evidence or acceptance unavailable"
        ]
        row["failure_messages"] = [
            failure_message(ValueError(cause)) for cause in causes
        ]


async def fallback_snapshot(
    session: aiohttp.ClientSession, token_file: Path, pipeline: str
) -> Row | None:
    try:
        async with control_command(session, token_file) as command:
            pipelines = await command("assist_pipeline/pipeline/list")
            engine = next(
                item["stt_engine"]
                for item in pipelines["pipelines"]
                if item["id"] == pipeline
            )
            registry = await command("config/entity_registry/list")
            entries = {
                item["config_entry_id"]
                for item in registry
                if item["entity_id"] == engine and item.get("platform") == "assist_live"
            }
            if len(entries) != 1:
                return None
        token = json.loads(token_file.read_text())["token"]
        async with asyncio.timeout(30):
            async with session.get(
                URL + "/api/diagnostics/config_entry/" + entries.pop(),
                headers={"Authorization": "Bearer " + token},
            ) as response:
                response.raise_for_status()
                data = (await response.json())["data"]
                counts = data["fallback_reasons"]
                turns = data.get("turns")
        if reason_delta({}, counts) is None or not isinstance(turns, list):
            return None
        return {
            "fallback_reasons": counts,
            "turns": turns,
            "timing_events_dropped": data.get("timing_events_dropped"),
            "counters_since_utc_ns": data.get("counters_since_utc_ns"),
        }
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        StopIteration,
        TimeoutError,
        aiohttp.ClientError,
    ):
        return None


def mapping(value: object) -> Row:
    # HA sends `data: null` for some pipeline events, for example `run-end`.
    return value if isinstance(value, dict) else {}


def pipeline_observations(row: Row) -> Row:
    events = row["events"]
    identifiers: Row = {}
    for event in events:
        data = mapping(event.get("data"))
        for source in (data, mapping(data.get("intent_output"))):
            for name in ("run_id", "conversation_id"):
                if source.get(name) is not None:
                    identifiers.setdefault(name, source[name])
    progress = [e for e in events if e["stage"] == "intent-progress"]
    deltas = [
        (e, mapping(mapping(e.get("data")).get("chat_log_delta"))) for e in progress
    ]
    text = [
        e
        for e, delta in deltas
        if delta.get("content") and delta.get("role", "assistant") == "assistant"
    ]
    calls = [e for e, delta in deltas if delta.get("tool_calls")]
    results = [e for e, delta in deltas if "tool_result" in delta]
    return {
        "test_only": True,
        "turn_id": row["turn_id"],
        "turn_id_source": row.get("turn_id_source"),
        "request_id": row["pipeline_request_id"],
        "run_id": identifiers.get("run_id"),
        "conversation_id": identifiers.get("conversation_id"),
        "first_agent_text_delta": text[0] if text else None,
        "tool_call_deltas": calls,
        "tool_result_deltas": results,
        "intent_end": next((e for e in events if e["stage"] == "intent-end"), None),
        "tts_start": "not_run",
    }


async def pipeline_trial(
    session: aiohttp.ClientSession,
    token_file: Path,
    pipeline: str,
    label: Row,
    pcm: bytes,
    stage: str,
    server: TestServer,
    *,
    stop_latency_seconds: float = 0,
    stop_margin_seconds: float = 0.02,
    device_id: str | None = None,
    turn_sensor: str | None = None,
    baselines: Row | None = None,
    stt_engines: Row | None = None,
) -> Row:
    row: Row = dict.fromkeys(RUN_FIELDS)
    row.update(
        schema_version=2,
        scope="simulated",
        mode="smoke",
        phase="p1",
        profile="safe",
        frozen=False,
        clip_id=label["id"],
        audio_sha256=label["audio_sha256"],
        split=label["split"],
        run_id="simulated-faults",
        baseline_config_id=pipeline,
        label=label,
        stage=stage,
        events=[],
        pipeline_request_id=3,
        agent_records=[],
        action_trace_complete=False,
        complete=False,
        failures=[],
        failure_messages=[],
        fault_injected=False,
        failure_cause=None,
        injected_fault=None,
        fallback_reasons_before=None,
        fallback_reasons_after=None,
        fallback_reasons_delta=None,
        fallback_turns=None,
        stop_latency_seconds=stop_latency_seconds,
        stop_margin_seconds=stop_margin_seconds,
        fallback_deadline_utc_ns=None,
        completed_utc_ns=None,
        baselines=baselines,
        stt_engines=stt_engines,
        baseline_paths={
            engine: {
                key: record.get(key)
                for key in ("interface", "pipeline_id", "engine_id")
            }
            for engine, record in (baselines or {}).items()
        },
        host_timing={"mark_start_utc_ns": time.time_ns()},
    )
    injection: asyncio.Task[None] | None = None
    sender: asyncio.Task[None] | None = None
    try:
        offset = fault_sample(
            label,
            stage,
            stop_latency_seconds=stop_latency_seconds,
            stop_margin_seconds=stop_margin_seconds,
        )
    except ValueError as error:
        row.update(status="unavailable", acceptable=False)
        row["failures"].append("FAULT_WINDOW_MARGIN_UNAVAILABLE")
        row["failure_messages"].append(failure_message(error))
        return row
    row["fault_planned_audio_sample"] = offset

    async def inject() -> None:
        row["fault_requested_utc_ns"] = time.time_ns()
        started = time.monotonic()
        await server.stop()
        stopped = time.monotonic()
        row["measured_stop_latency_seconds"] = stopped - started
        row["fault_injected"] = True
        row["fault_stopped_utc_ns"] = time.time_ns()
        window = fault_window(label, stage)
        if window is not None:
            position = int((stopped - row["audio_started_monotonic"]) * 16000)
            row["fault_stopped_audio_sample"] = position
            row["fault_window_samples"] = list(window)
            if not window[0] <= position < window[1]:
                row["fault_injected"] = False
                row["failures"].append("FAULT_STAGE_MISSED")
        row["injected_fault"] = "server_container_stopped"

    try:
        before = await fallback_snapshot(session, token_file, pipeline)
        loss_start = await capture_loss(
            None, satellites=[device_id or "not_identified"], integration_data=before
        )
        row["loss_counters"] = {"start": loss_start, "end": None}
        row["loss_unavailable"] = True
        row["fallback_reasons_before"] = before["fallback_reasons"] if before else None
        if stage == "before":
            await inject()
        async with await connect(session, token_file) as ws:
            previous_turn = None
            if turn_sensor:
                await ws.send_json({"id": 1, "type": "get_states"})
                state_response = await ws.receive_json()
                if state_response.get("success") is not True:
                    raise ValueError("pre-run sensor snapshot unavailable")
                previous_turn = next(
                    state["attributes"].get("turn_id")
                    for state in state_response["result"]
                    if state["entity_id"] == turn_sensor
                )
            await ws.send_json(
                {
                    "id": 2,
                    "type": "subscribe_events",
                    "event_type": "assist_live_test_record",
                }
            )
            if (await ws.receive_json()).get("success") is not True:
                raise ValueError("operation recorder subscription failed")
            await ws.send_json(
                {
                    "id": 3,
                    "type": "assist_pipeline/run",
                    "pipeline": pipeline,
                    "start_stage": "stt",
                    "end_stage": "intent",
                    "input": {"sample_rate": 16000},
                    "device_id": device_id,
                }
            )

            async def send_audio(handler: int) -> None:
                nonlocal injection
                started = time.monotonic()
                row["audio_started_monotonic"] = started
                for position in range(0, len(pcm) + 32000 * 2, 640):
                    if (
                        offset is not None
                        and position // 2 >= offset
                        and injection is None
                    ):
                        row["fault_audio_sample"] = position // 2
                        injection = asyncio.create_task(inject())
                    await ws.send_bytes(
                        bytes([handler])
                        + (
                            pcm[position : position + 640]
                            if position < len(pcm)
                            else bytes(640)
                        )
                    )
                    await asyncio.sleep(
                        max(0, started + (position + 640) / 32000 - time.monotonic())
                    )
                await ws.send_bytes(bytes([handler]))

            async with asyncio.timeout(len(pcm) / 32000 + 20 + 15):
                while True:
                    message = await ws.receive_json()
                    if message.get("type") == "result" and message.get("id") == 3:
                        if message.get("success") is not True:
                            raise ValueError("pipeline run refused")
                    event = message.get("event", {})
                    if event.get("event_type") == "assist_live_test_record":
                        row.setdefault("agent_records", []).append(event["data"])
                    if message.get("id") != 3 or message.get("type") != "event":
                        if message.get("id") == 4 and message.get("type") == "result":
                            if turn_sensor and message.get("success") is True:
                                states = [
                                    state
                                    for state in message["result"]
                                    if state["entity_id"] == turn_sensor
                                ]
                                if len(states) == 1:
                                    attributes = states[0]["attributes"]
                                    if (
                                        attributes.get("turn_type") == "voice"
                                        and attributes.get("turn_id") != previous_turn
                                    ):
                                        row["turn_id"] = attributes.get("turn_id")
                                        row["turn_id_source"] = turn_sensor
                            break
                        continue
                    kind = event.get("type")
                    row["events"].append(
                        {
                            "stage": kind,
                            "data": mapping(event.get("data")),
                            "t_utc_ns": time.time_ns(),
                            "source": "ha",
                            "request_id": message["id"],
                            "test_only": True,
                            "quality": "measured",
                        }
                    )
                    if kind == "run-start":
                        row["fallback_deadline_utc_ns"] = (
                            row["events"][-1]["t_utc_ns"] + FALLBACK_LIMIT_NS
                        )
                        handler = event["data"]["runner_data"]["stt_binary_handler_id"]
                        sender = asyncio.create_task(send_audio(handler))
                    elif kind == "stt-end":
                        row["recognized_transcript"] = event["data"]["stt_output"][
                            "text"
                        ]
                        if stage == "after-final":
                            injection = asyncio.create_task(inject())
                    elif kind == "error":
                        row["failures"].append(
                            row["events"][-1]["data"].get("code", "pipeline_error")
                        )
                        row["failure_messages"].append(
                            failure_message(ValueError(str(row["events"][-1]["data"])))
                        )
                    elif kind == "run-end":
                        row["completed_utc_ns"] = time.time_ns()
                        await ws.send_json({"id": 4, "type": "get_states"})
            records = [
                record
                for record in row.get("agent_records", [])
                if record.get("recognized_transcript") == row["recognized_transcript"]
                and (device_id is None or record.get("device_id") == device_id)
            ]
            if len(records) == 1:
                record = records[0]
                row.update(
                    turn_id=row["turn_id"] or record.get("turn_id"),
                    evaluated_intent=record.get("selected_intent"),
                    action_trace_complete=record.get("completed") is True,
                    reply_text=record.get("reply"),
                )
                if record.get("turn_id") and record["turn_id"] != row["turn_id"]:
                    row["action_trace_complete"] = False
                    row["failures"].append("TURN_LINK_MISMATCH")
                if isinstance(record.get("operations"), list):
                    row["action_trace"] = [
                        {
                            target: operation[source]
                            for source, target in BRIDGE_OPERATION_MAP.items()
                        }
                        for operation in record["operations"]
                    ]
            empty = no_text_error(row)
            if empty:
                row.update(action_trace=[], action_trace_complete=True, operations=[])
            if not row["action_trace_complete"]:
                row["failures"].append("MISSING_COMPLETE_OPERATION_RECORD")
            if not empty and not isinstance(row["recognized_transcript"], str):
                row["failures"].append("MISSING_TRANSCRIPT")
            if not empty and not isinstance(row["evaluated_intent"], dict):
                row["failures"].append("MISSING_SELECTED_INTENT")
            if not row["turn_id"] and stage != "fallback-baseline":
                row["failures"].append("MISSING_TURN_ID")
    except (
        ValueError,
        KeyError,
        TypeError,
        TimeoutError,
        aiohttp.ClientError,
    ) as error:
        row["failures"].append(type(error).__name__)
        row["failure_messages"].append(failure_message(error))
    finally:
        if sender is not None:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        try:
            if injection is not None:
                try:
                    await injection
                except (ValueError, TimeoutError) as error:
                    row["failures"].append("FAULT_INJECTION_FAILED")
                    row["failure_messages"].append(failure_message(error))
        finally:
            try:
                after = await fallback_snapshot(session, token_file, pipeline)
                loss_end = await capture_loss(
                    None,
                    satellites=[device_id or "not_identified"],
                    integration_data=after,
                )
                row["loss_counters"] = loss_window(
                    row["loss_counters"]["start"], loss_end
                )
                for flag in LOSS_FLAGS:
                    row[flag] = row["loss_counters"][flag]
                row["fallback_reasons_after"] = (
                    after["fallback_reasons"] if after else None
                )
                row["fallback_turns"] = after["turns"] if after else None
                delta = reason_delta(
                    row["fallback_reasons_before"], row["fallback_reasons_after"]
                )
                row["fallback_reasons_delta"] = delta
                _, row["failure_cause"] = stt_reason_for_turn(row)
                if stage != "fallback-baseline" and not recorded_reason_matches(row):
                    row["failures"].append("MISSING_OR_UNEXPECTED_FALLBACK_REASON")
            finally:
                await server.restore()
    if stage in ("baseline", "fallback-baseline"):
        return row
    row["transcript_engine"] = transcript_engine(row)
    row["status"] = trial_status(row)
    row["acceptable"] = row["status"] == "passed"
    if row["status"] == "unavailable":
        row["failures"].append("BASELINE_UNAVAILABLE")
    for event in row["events"]:
        event["turn_id"] = row["turn_id"]
    row["test_pipeline"] = pipeline_observations(row)
    if not row["acceptable"] and not row["failures"]:
        row["failures"].append("INCORRECT_TRANSCRIPT_OR_OPERATIONS")
    if not row["acceptable"]:
        complete_failure_messages(row)
    return row


async def baseline_engines(
    session: aiohttp.ClientSession, token_file: Path, pipeline: str
) -> Row:
    async with control_command(session, token_file) as command:
        pipelines = await command("assist_pipeline/pipeline/list")
        selected = next(
            (item for item in pipelines["pipelines"] if item["id"] == pipeline), None
        )
        if selected is None:
            raise ValueError("baseline pipeline unavailable")
        registry = await command("config/entity_registry/list")
        entries = [
            item["config_entry_id"]
            for item in registry
            if item["entity_id"] == selected["stt_engine"]
            and item.get("platform") == "assist_live"
        ]
    if len(entries) != 1:
        raise ValueError("unique assist-live STT entry unavailable")
    token = json.loads(token_file.read_text())["token"]
    headers = {"Authorization": "Bearer " + token}
    flow_id = None
    error: BaseException | None = None
    async with asyncio.timeout(30):
        try:
            async with session.post(
                URL + "/api/config/config_entries/options/flow",
                headers=headers,
                json={"handler": entries[0]},
            ) as response:
                response.raise_for_status()
                form = await response.json()
            if not isinstance(form, dict):
                raise ValueError("initial options form unavailable")
            flow_id = form.get("flow_id")
            if (
                form.get("type") != "form"
                or not isinstance(flow_id, str)
                or not flow_id
            ):
                raise ValueError("initial options form unavailable")
            fallback = next(
                (
                    field.get("default")
                    for field in form["data_schema"]
                    if isinstance(field, dict) and field.get("name") == "fallback_stt"
                ),
                None,
            )
            if not isinstance(fallback, str) or not fallback.startswith("stt."):
                raise ValueError("fallback STT entity unavailable")
            return {
                "server": selected["stt_engine"],
                "fallback": fallback,
                "language": selected["stt_language"],
            }
        except BaseException as caught:
            error = caught
            raise
        finally:
            if isinstance(flow_id, str) and flow_id:
                try:
                    async with session.delete(
                        URL + "/api/config/config_entries/options/flow/" + flow_id,
                        headers=headers,
                    ) as response:
                        response.raise_for_status()
                except Exception as cleanup:
                    # A failed delete must not hide the first error.
                    if error is None:
                        raise
                    error.add_note(
                        "options flow delete failed: " + type(cleanup).__name__
                    )


async def collect_baselines(
    session: aiohttp.ClientSession,
    token_file: Path,
    pipeline: str,
    label: Row,
    pcm: bytes,
    engines: Row,
    server: TestServer,
    *,
    device_id: str | None = None,
    turn_sensor: str | None = None,
) -> Row:
    baselines: Row = {}
    for engine in ("server", "fallback"):
        record: Row = {
            "engine_id": engines.get(engine),
            "clip_id": label["id"],
            "audio_sha256": label["audio_sha256"],
            "status": "unavailable",
            "transcript": None,
            "pipeline_id": pipeline if engine == "server" else None,
            "interface": "assist_pipeline/run",
        }
        baselines[engine] = record
        try:
            if not engines.get(engine):
                raise ValueError("baseline engine unavailable")
            if engine == "server":
                row = await pipeline_trial(
                    session,
                    token_file,
                    pipeline,
                    label,
                    pcm,
                    "baseline",
                    server,
                    device_id=device_id,
                    turn_sensor=turn_sensor,
                )
                record["evidence"] = row
                if row["failures"] or transcript_engine(row) != "server":
                    raise ValueError("no-fault server baseline failed")
                text = row["recognized_transcript"]
            else:

                async def fresh_command(kind: str, **fields: Any) -> Any:
                    async with control_command(session, token_file) as fresh:
                        return await fresh(kind, **fields)

                record["pipeline_cleanup"] = {}
                async with control_command(session, token_file) as command:
                    async with temporary_pipeline(
                        command,
                        record["pipeline_cleanup"],
                        pipeline,
                        cleanup_command=fresh_command,
                        stt_engine=engines[engine],
                        redactions=(json.loads(token_file.read_text())["token"],),
                    ) as (baseline_pipeline, _, _):
                        record["pipeline_id"] = baseline_pipeline
                        row = await pipeline_trial(
                            session,
                            token_file,
                            baseline_pipeline,
                            label,
                            pcm,
                            "fallback-baseline",
                            server,
                            device_id=device_id,
                        )
                        record["evidence"] = row
                        if row["failures"]:
                            raise ValueError("no-fault fallback baseline failed")
                        text = row["recognized_transcript"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("baseline transcript unavailable")
            record.update(status="available", transcript=text)
        except TemporaryPipelineError:
            raise
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            TimeoutError,
            aiohttp.ClientError,
        ) as error:
            record["error"] = type(error).__name__
            record["message"] = failure_message(error)
    return baselines


@asynccontextmanager
async def temporary_pipeline(
    command: Command,
    result: Row,
    source_id: str,
    *,
    cleanup_command: Command,
    redactions: tuple[str, ...] = (),
    stt_engine: str | None = None,
) -> AsyncIterator[tuple[str, str, str]]:
    async def snapshot(call: Command) -> Row:
        pipelines = await call("assist_pipeline/pipeline/list")
        states = await call("get_states")
        return {
            "pipelines": pipelines,
            "selects": sorted(
                [
                    {key: state[key] for key in ("entity_id", "state", "attributes")}
                    for state in states
                    if state["entity_id"].startswith("select.")
                ],
                key=lambda state: state["entity_id"],
            ),
        }

    name = FALLBACK_BASELINE_PIPELINE_NAME if stt_engine else TEMPORARY_PIPELINE_NAME
    result["bench_before"] = await snapshot(command)
    result["bench_restored"] = False
    leftovers = [
        pipeline["id"]
        for pipeline in result["bench_before"]["pipelines"]["pipelines"]
        if pipeline.get("name") == name
    ]
    if leftovers:
        raise TemporaryPipelineError(
            "TEMPORARY_PIPELINE_EXISTS: " + ",".join(leftovers) + " (" + name + ")"
        )
    created = None
    try:
        source = next(
            pipeline
            for pipeline in result["bench_before"]["pipelines"]["pipelines"]
            if pipeline["id"] == source_id
        )
        states = await command("get_states")
        device_id = next(
            state["attributes"]["virtual_device_id"]
            for state in states
            if state["entity_id"] == "conversation.assist_live_short"
        )
        registry = await command("config/entity_registry/list")
        stt_device = next(
            entity["device_id"]
            for entity in registry
            if entity["entity_id"] == source["stt_engine"]
        )
        sensors = [
            entity["entity_id"]
            for entity in registry
            if stt_device
            and entity.get("device_id") == stt_device
            and entity.get("platform") == "assist_live"
            and any(
                state["entity_id"] == entity["entity_id"]
                and "turn_type" in state["attributes"]
                for state in states
            )
        ]
        if len(sensors) != 1:
            raise ValueError("unique pipeline STT turn sensor unavailable")
        fields = {key: source[key] for key in pipeline_fields("", "")}
        if stt_engine:
            fields.update(name=name, stt_engine=stt_engine)
        else:
            fields.update(
                name=name,
                conversation_engine="conversation.assist_live_short",
                prefer_local_intents=False,
            )
        created = await command("assist_pipeline/pipeline/create", **fields)
        result["temporary_pipeline"] = created["id"]
        yield created["id"], device_id, sensors[0]
    finally:

        async def cleanup() -> None:
            error = None
            result["cleanup_attempt_errors"] = []
            pipeline_id = created["id"] if created is not None else source_id
            if created is not None:
                for attempt in range(1, 3):
                    try:
                        async with asyncio.timeout(PIPELINE_CLEANUP_TIMEOUT):
                            await cleanup_command(
                                "assist_pipeline/pipeline/delete",
                                pipeline_id=pipeline_id,
                            )
                        break
                    except Exception as failure:
                        result["cleanup_attempt_errors"].append(
                            {
                                "attempt": attempt,
                                "class": type(failure).__name__,
                                "message": scrub_cleanup_error(failure, redactions),
                            }
                        )
                else:
                    error = TemporaryPipelineError(
                        "TEMPORARY_PIPELINE_DELETE_FAILED: " + pipeline_id
                    )
            try:
                async with asyncio.timeout(PIPELINE_CLEANUP_TIMEOUT):
                    result["bench_after"] = await snapshot(cleanup_command)
            except Exception as failure:
                result["bench_after"] = None
                result["cleanup_snapshot_error"] = {
                    "class": type(failure).__name__,
                    "message": scrub_cleanup_error(failure, redactions),
                }
                error = error or TemporaryPipelineError(
                    "TEMPORARY_PIPELINE_SNAPSHOT_FAILED: " + pipeline_id
                )
            result["bench_restored"] = result["bench_before"] == result["bench_after"]
            if not result["bench_restored"]:
                error = error or TemporaryPipelineError(
                    "TEMPORARY_PIPELINE_SNAPSHOT_MISMATCH: " + pipeline_id
                )
            if error is not None:
                result["cleanup_error"] = str(error)
                raise error

        task = asyncio.create_task(cleanup())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError


def validate_output(
    path: Path, *, simulated: bool, check: bool, hardware: bool = False
) -> None:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT):
        return
    if simulated:
        raise ValueError("SIMULATED_OUTPUT_IN_REPOSITORY")
    if hardware:
        raise ValueError("HARDWARE_OUTPUT_IN_REPOSITORY")
    if (
        not check
        or resolved.parent != ROOT / "docs/reports"
        or resolved.suffix != ".json"
    ):
        raise ValueError("CHECKED_REPORT_OUTPUT_REQUIRED")


async def run(args: argparse.Namespace) -> Row:
    validate_output(
        args.output,
        simulated=args.simulate,
        check=args.check,
        hardware=getattr(args, "hardware", False),
    )
    from tools.fault_hardware import Hardware

    hardware = Hardware(args) if getattr(args, "hardware", False) else None
    if hardware is not None:
        async with aiohttp.ClientSession() as session:
            async with control_command(session, args.token_file) as command:
                await hardware.preflight(command)
    labels = load_corpus(args.labels)
    if args.native_matrix:
        validate_matrix_document(args.native_matrix.read_text())
    result: Row = {
        "schema_version": 2,
        "scope": "hardware" if hardware else "simulated",
        "trials": [],
        "attempts": [],
        "stage_status": {
            stage: {
                "status": "NOT RUN",
                "reason": (
                    "no labelled pause recordings"
                    if stage == "pause"
                    and not any(label.get("set") == "pause" for label in labels)
                    else "not completed"
                    if stage in args.stages.split(",")
                    else "not selected"
                ),
            }
            for stage in STAGES
        },
    }
    plans: dict[str, list[tuple[Row, bytes]]] = {}
    for stage in args.stages.split(","):
        candidates = [
            label
            for label in labels
            if label.get("split") == "held_out"
            and (stage != "pause" or label.get("set") == "pause")
        ]
        if not candidates:
            result["preflight_failed"] = True
            result["stage_status"][stage] = {
                "status": "NOT RUN",
                "reason": "no labelled recordings or fault boundaries",
            }
            write_result(args.output, result)
            print(
                f"NOT RUN {stage}: no labelled recordings or fault boundaries; "
                "no bench changes"
            )
            return result
        plans[stage] = []
        for index in range(args.each):
            label = candidates[index % len(candidates)]
            try:
                fault_window(label, stage)
                pcm = audio_input(label, args.audio_root)
            except OSError, ValueError:
                result["preflight_failed"] = True
                result["stage_status"][stage] = {
                    "status": "NOT RUN",
                    "reason": "invalid audio or missing fault boundary",
                }
                write_result(args.output, result)
                print(
                    f"NOT RUN {stage}: invalid audio or missing fault boundary; "
                    "no bench changes"
                )
                return result
            plans[stage].append((label, pcm))
    server = TestServer()
    samples: list[float] = []

    async def calibrate_stop() -> None:
        result["stop_latency_samples_seconds"] = []
        for _ in range(STOP_CALIBRATION_SAMPLES if "speech" in plans else 1):
            sample = await server.measure_stop_latency()
            if not math.isfinite(sample) or sample < 0:
                raise ValueError("invalid stop latency")
            samples.append(sample)
            result["stop_latency_samples_seconds"] = samples
            write_result(args.output, result)
        result["stop_latency_seconds"] = statistics.median(samples)
        result["stop_margin_seconds"] = (max(samples) - min(samples)) / 2 + 0.02

    try:
        await server.restore()
        if not hardware:
            await calibrate_stop()
        async with aiohttp.ClientSession() as session:

            async def trials(
                pipeline: str, device_id: str | None = None, sensor: str | None = None
            ) -> None:
                try:
                    engines = await baseline_engines(session, args.token_file, pipeline)
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    StopIteration,
                    TimeoutError,
                    aiohttp.ClientError,
                ) as error:
                    engines = {}
                    result["baseline_engine_error"] = type(error).__name__
                result["stt_engines"] = engines
                if hardware:
                    from tools.fault_hardware import qualification_failure

                    originals = {
                        label["id"]: label
                        for planned in plans.values()
                        for label, _ in planned
                    }
                    target = len(originals)
                    required = {
                        stage: len({label["id"] for label, _ in planned})
                        for stage, planned in plans.items()
                    }
                    pools = {
                        stage: [
                            label
                            for label in labels
                            if label.get("split") == "held_out"
                            and (stage != "pause" or label.get("set") == "pause")
                        ]
                        for stage in plans
                    }
                    pending = list(originals.values())
                    limit = getattr(args, "qualification_limit", None)
                    if limit is None:
                        limit = 2 * target
                    qualification: Row = {
                        "target": target,
                        "required_by_stage": required,
                        "limit": limit,
                        "candidates": 0,
                        "eligible": 0,
                        "recordings": [],
                        "sufficient": False,
                    }
                    result["qualification"] = qualification
                    eligible: dict[str, tuple[Row, bytes]] = {}
                    played: set[str] = set()
                    while qualification["candidates"] < limit:
                        if pending:
                            label = pending.pop(0)
                        else:
                            unmet = [
                                stage
                                for stage, pool in pools.items()
                                if sum(label["id"] in eligible for label in pool)
                                < required[stage]
                            ]
                            candidate = next(
                                (
                                    label
                                    for stage in unmet or pools
                                    for label in pools[stage]
                                    if label["id"] not in played
                                ),
                                None,
                            )
                            if candidate is None:
                                break
                            label = candidate
                        played.add(label["id"])
                        qualification["candidates"] += 1
                        try:
                            for stage, pool in pools.items():
                                if label in pool:
                                    fault_window(label, stage)
                            pcm = audio_input(label, args.audio_root)
                        except (OSError, ValueError) as error:
                            row = {
                                "clip_id": label["id"],
                                "failures": [type(error).__name__],
                                "failure_messages": [failure_message(error)],
                            }
                            cause: str | None = "unavailable"
                        else:
                            row = await hardware.trial(
                                session,
                                args.token_file,
                                pipeline,
                                label,
                                pcm,
                                "qualification",
                                server,
                                stop_latency_seconds=0,
                                stop_margin_seconds=0.02,
                                sensor=sensor or "",
                            )
                            await hardware.baselines(
                                session,
                                args.token_file,
                                pipeline,
                                label,
                                pcm,
                                engines,
                                server,
                                row,
                                sensor or "",
                            )
                            row["stt_engines"] = engines
                            cause = qualification_failure(row)
                        row.update(eligible=cause is None, cause=cause)
                        if cause is None:
                            eligible[label["id"]] = (label, pcm)
                        else:
                            complete_failure_messages(row)
                        qualification["recordings"].append(row)
                        qualification["eligible"] = len(eligible)
                        qualification["sufficient"] = len(eligible) >= target and all(
                            sum(label["id"] in eligible for label in pools[stage])
                            >= count
                            for stage, count in required.items()
                        )
                        write_result(args.output, result)
                        if qualification["sufficient"]:
                            break
                    if not qualification["sufficient"]:
                        result["preflight_failed"] = True
                        for stage in plans:
                            result["stage_status"][stage] = {
                                "status": "NOT RUN",
                                "reason": "insufficient_eligible_recordings",
                            }
                            count = sum(
                                label["id"] in eligible for label in pools[stage]
                            )
                            print(
                                f"NOT RUN {stage}: insufficient eligible recordings "
                                f"({count}/{required[stage]})",
                                flush=True,
                            )
                        return
                    if getattr(args, "qualify_only", False):
                        return
                    for stage, pool in pools.items():
                        available = [
                            eligible[label["id"]]
                            for label in pool
                            if label["id"] in eligible
                        ]
                        plans[stage] = [
                            available[index % len(available)]
                            for index in range(args.each)
                        ]
                    await calibrate_stop()
                cache: Row = {}
                for planned in [] if hardware else plans.values():
                    for label, pcm in planned:
                        if label["id"] not in cache:
                            cache[label["id"]] = await collect_baselines(
                                session,
                                args.token_file,
                                pipeline,
                                label,
                                pcm,
                                engines,
                                server,
                                device_id=device_id,
                                turn_sensor=sensor,
                            )
                result["baselines"] = cache
                if hardware and any(stage in plans for stage in ("speech", "pause")):
                    label, pcm = next(iter(plans.values()))[0]
                    result["playback_calibration_attempts"] = []
                    for _ in range(MAX_SLOT_ATTEMPTS):
                        calibration = await hardware.trial(
                            session,
                            args.token_file,
                            pipeline,
                            label,
                            pcm,
                            "baseline",
                            server,
                            stop_latency_seconds=result["stop_latency_seconds"],
                            stop_margin_seconds=result["stop_margin_seconds"],
                            sensor=sensor or "",
                        )
                        result["playback_calibration_attempts"].append(calibration)
                        write_result(args.output, result)
                        if (calibration.get("correlation") or {}).get("complete"):
                            break
                for stage in args.stages.split(","):
                    for index, (label, pcm) in enumerate(plans[stage]):
                        for attempt in range(1, MAX_SLOT_ATTEMPTS + 1):
                            used_samples = list(samples)
                            latency = statistics.median(used_samples)
                            margin = (max(used_samples) - min(used_samples)) / 2 + 0.02
                            if hardware:
                                row = await hardware.trial(
                                    session,
                                    args.token_file,
                                    pipeline,
                                    label,
                                    pcm,
                                    stage,
                                    server,
                                    stop_latency_seconds=latency,
                                    stop_margin_seconds=margin,
                                    sensor=sensor or "",
                                )
                                await hardware.baselines(
                                    session,
                                    args.token_file,
                                    pipeline,
                                    label,
                                    pcm,
                                    engines,
                                    server,
                                    row,
                                    sensor or "",
                                )
                                row["stt_engines"] = engines
                                result["playback_delay_samples_seconds"] = list(
                                    hardware.delay_samples
                                )
                            else:
                                row = await pipeline_trial(
                                    session,
                                    args.token_file,
                                    pipeline,
                                    label,
                                    pcm,
                                    stage,
                                    server,
                                    stop_latency_seconds=latency,
                                    stop_margin_seconds=margin,
                                    device_id=device_id,
                                    turn_sensor=sensor,
                                    baselines=cache[label["id"]],
                                    stt_engines=engines,
                                )
                            row.update(
                                trial_slot=index + 1,
                                attempt=attempt,
                                stop_latency_seconds=latency,
                                stop_margin_seconds=margin,
                                stop_latency_samples_seconds=used_samples,
                            )
                            sample = row.get("measured_stop_latency_seconds")
                            if sample is not None:
                                if not math.isfinite(sample) or sample < 0:
                                    raise ValueError("invalid stop latency")
                                samples.append(sample)
                            result["stop_latency_seconds"] = statistics.median(samples)
                            result["stop_margin_seconds"] = (
                                max(samples) - min(samples)
                            ) / 2 + 0.02
                            missed = injection_status(row) == "invalid_injection"
                            row["status"] = trial_status(row)
                            row["acceptable"] = row["status"] == "passed"
                            if not row["acceptable"]:
                                complete_failure_messages(row)
                            result["attempts"].append(row)
                            if not missed:
                                result["trials"].append(row)
                            elif attempt == MAX_SLOT_ATTEMPTS:
                                result["trials"].append(
                                    {
                                        **row,
                                        "status": "unavailable",
                                        "failures": [
                                            *row["failures"],
                                            "FAULT_ATTEMPTS_EXHAUSTED",
                                        ],
                                    }
                                )
                            write_result(args.output, result)
                            print(
                                f"{row['status'].upper()} {result['scope']} {stage} "
                                f"{index + 1} attempt {attempt}: "
                                + ",".join(row["failures"]),
                                flush=True,
                            )
                            if not missed:
                                break
                        result["stage_status"][stage] = {
                            "status": "RECORDED",
                            "trials": index + 1,
                            **result["faults"]["stages"][stage],
                        }
                        write_result(args.output, result)

            if args.temporary_agent_pipeline:

                async def fresh_command(kind: str, **fields: Any) -> Any:
                    async with control_command(session, args.token_file) as fresh:
                        return await fresh(kind, **fields)

                async with control_command(session, args.token_file) as command:
                    async with temporary_pipeline(
                        command,
                        result,
                        args.pipeline,
                        cleanup_command=fresh_command,
                        redactions=(json.loads(args.token_file.read_text())["token"],),
                    ) as (
                        pipeline,
                        device_id,
                        sensor,
                    ):
                        if hardware:
                            async with hardware.selected(
                                fresh_command, pipeline, result
                            ):
                                await trials(pipeline, hardware.device_id, sensor)
                        else:
                            await trials(pipeline, device_id, sensor)
            else:
                await trials(args.pipeline)
        if args.native_matrix:
            result["native_device"] = run_native_matrix()
            result["native_errors"] = validate_native_matrix(result["native_device"])
            result["native_virtual"] = []
            for path in ("server_present", "server_absent"):
                if path == "server_absent":
                    await server.stop()
                else:
                    await server.restore()
                rows = await run_virtual(args.token_file, args.labels)
                result["native_virtual"].extend({**row, "path": path} for row in rows)
                print(
                    f"NOTE {path}: virtual rows {len(rows)}; device rows unobserved",
                    flush=True,
                )
    finally:
        result["restored_healthy"] = False
        try:
            await server.restore()
            result["restored_healthy"] = True
        finally:
            write_result(args.output, result)
    return result


def write_result(path: Path, result: Row) -> None:
    windows = [
        row["loss_counters"]
        for row in result.get("trials", [])
        if row.get("loss_counters")
    ]
    if windows:
        result["loss_counters"] = loss_window(windows[0]["start"], windows[-1]["end"])
        for flag in LOSS_FLAGS:
            result[flag] = result["loss_counters"][flag] or any(
                window.get(flag) for window in windows
            )
    result["faults"] = fault_gate(result)
    result["native_denominators"] = {
        "device": {
            "count": sum(
                row.get("observation") is not None
                for row in result.get("native_device", [])
            ),
            "denominator": len(result.get("native_device", [])),
            "measure": "observed rows",
        },
        "virtual": {
            "count": sum(
                row.get("passed") is True for row in result.get("native_virtual", [])
            ),
            "denominator": len(result.get("native_virtual", [])),
            "measure": "satisfactory rows",
        },
    }
    with path.open("w", encoding="utf-8") as output:
        os.chmod(path, 0o600)
        output.write(json.dumps(result, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--each", type=int, default=10)
    parser.add_argument("--stages", default=",".join(STAGES))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--hardware", action="store_true")
    parser.add_argument(
        "--qualification-limit",
        type=int,
        help="candidate limit (default: twice the distinct trial recordings)",
    )
    parser.add_argument("--qualify-only", action="store_true")
    parser.add_argument("--satellite")
    parser.add_argument("--playback-sink")
    parser.add_argument("--wake-audio", type=Path)
    parser.add_argument("--ha-container")
    parser.add_argument("--turn-audio-archive", type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--pipeline")
    parser.add_argument("--labels", type=Path, default=Path("tests/data/labels.jsonl"))
    parser.add_argument("--audio-root", type=Path)
    parser.add_argument("--native-matrix", type=Path)
    parser.add_argument("--temporary-agent-pipeline", action="store_true")
    args = parser.parse_args(argv)
    if (
        args.qualify_only or args.qualification_limit is not None
    ) and not args.hardware:
        parser.error("qualification options require --hardware")
    if args.qualification_limit is not None and args.qualification_limit <= 0:
        parser.error("qualification limit must be positive")
    if args.hardware and (
        args.simulate
        or args.native_matrix
        or not args.temporary_agent_pipeline
        or not all(
            (
                args.satellite,
                args.playback_sink,
                args.wake_audio,
                args.ha_container,
                args.turn_audio_archive,
                args.token_file,
                args.pipeline,
                args.audio_root,
            )
        )
    ):
        parser.error(
            "hardware requires --satellite, --playback-sink, --wake-audio, "
            "--ha-container, --turn-audio-archive, --token-file, --pipeline, "
            "--audio-root, and --temporary-agent-pipeline; "
            "do not combine with --simulate or --native-matrix"
        )
    try:
        validate_output(
            args.output,
            simulated=args.simulate,
            check=args.check,
            hardware=args.hardware,
        )
    except ValueError as error:
        print("FAIL " + str(error))
        return 1
    if args.native_matrix and not args.simulate:
        return native_main(
            [
                "--matrix",
                str(args.native_matrix),
                "--output",
                str(args.output),
                *(["--check"] if args.check else []),
            ]
        )
    if (
        not (args.simulate or args.hardware)
        or not args.token_file
        or not args.pipeline
        or not args.audio_root
    ):
        parser.error(
            "use --simulate with --token-file, --pipeline, and --audio-root; "
            "or use --hardware with its explicit bench arguments"
        )
    if (
        args.each <= 0
        or not args.stages
        or len(args.stages.split(",")) != len(set(args.stages.split(",")))
        or not set(args.stages.split(",")) <= set(STAGES)
    ):
        parser.error("positive counts and unique known stages are required")
    try:
        result = asyncio.run(run(args))
        if args.qualify_only:
            qualification = result.get("qualification", {})
            print(
                f"NOTE hardware qualification {qualification.get('eligible', 0)}/"
                f"{qualification.get('candidates', 0)}; "
                f"sufficient {qualification.get('sufficient', False)}; "
                "hardware acceptance NOT RUN"
            )
            return int(qualification.get("sufficient") is not True)
        summary = fault_gate(result)
        print(
            f"NOTE {'hardware' if args.hardware else 'simulated'} faults "
            f"{summary['count']}/{summary['denominator']}; "
            + (
                "hardware gate " + str(summary["passed"])
                if args.hardware
                else "hardware acceptance NOT RUN"
            )
        )
        for stage, counts in summary["stages"].items():
            print(f"NOTE {stage}: " + json.dumps(counts, sort_keys=True))
        return int(
            result.get("preflight_failed") is True
            or args.check
            and (not fault_gate(result)["passed"] or bool(result.get("native_errors")))
        )
    except TemporaryPipelineError as error:
        print("FAIL " + str(error))
        return 1
    except OSError, ValueError, aiohttp.ClientError, TimeoutError:
        print("FAIL isolated fault run; inspect the private trial records")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
