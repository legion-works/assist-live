import argparse
import asyncio
import hashlib
import json
import math
import os
import re
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
from tools.test_environment import FIXTURES, URL, expected_operations

Row = dict[str, Any]
STAGES = ("before", "speech", "pause", "after-final")
CONTAINER = "assist-live-server-q1"


def acceptable(
    trial: Row | None = None,
    *,
    correct: bool = False,
    empty: bool = False,
    action_count: int = 0,
) -> bool:
    # Flags alone cannot prove transcript or operation correctness.
    if trial is None:
        return False
    transcript = trial.get("recognized_transcript")
    selected = trial.get("evaluated_intent")
    trace = trial.get("action_trace")
    label = trial.get("label")
    deadline = trial.get("fallback_deadline_utc_ns")
    completed = trial.get("completed_utc_ns")
    if not (
        isinstance(transcript, str)
        and isinstance(selected, dict)
        and isinstance(selected.get("name"), str)
        and selected["name"]
        and isinstance(selected.get("slots"), dict)
        and isinstance(trace, list)
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
        and isinstance(label.get("transcript"), str)
        and isinstance(label.get("intent"), dict)
        and trial.get("stage") in STAGES
        and trial.get("fault_injected") is True
        and isinstance(trial.get("failure_cause"), str)
        and trial["failure_cause"]
        and type(deadline) is int
        and type(completed) is int
        and 0 < completed <= deadline
    ):
        return False
    if transcript == "":
        return trace == [] and trial.get("operations", []) == []
    if re.findall(r"\w+", transcript.casefold()) != re.findall(
        r"\w+", label["transcript"].casefold()
    ):
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
            and selected == label["intent"]
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
    from tools.report import rate

    trials = faults.get("trials", [])
    passed = sum(acceptable(trial) for trial in trials)
    counts = Counter(trial.get("stage") for trial in trials)
    return {
        **rate(passed, len(trials)),
        "passed": len(trials) == 40
        and len({trial.get("turn_id") for trial in trials}) == 40
        and passed == 40
        and counts == {stage: 10 for stage in STAGES}
        and not any(trial.get("scope") == "simulated" for trial in trials),
    }


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


def fault_sample(label: Row, stage: str) -> int | None:
    window = fault_window(label, stage)
    return (window[0] + window[1]) // 2 if window else None


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
    ws = await session.ws_connect(URL + "/api/websocket")
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


async def pipeline_trial(
    session: aiohttp.ClientSession,
    token_file: Path,
    pipeline: str,
    label: Row,
    pcm: bytes,
    stage: str,
    server: TestServer,
    deadline_seconds: float,
    *,
    device_id: str | None = None,
    turn_sensor: str | None = None,
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
        action_trace_complete=False,
        complete=False,
        failures=[],
        fault_injected=False,
        failure_cause=None,
        fallback_deadline_utc_ns=None,
        completed_utc_ns=None,
        host_timing={"mark_start_utc_ns": time.time_ns()},
    )
    injection: asyncio.Task[None] | None = None
    sender: asyncio.Task[None] | None = None
    offset = fault_sample(label, stage)

    async def inject() -> None:
        row["fault_requested_utc_ns"] = time.time_ns()
        await server.stop()
        row["fault_injected"] = True
        row["fault_stopped_utc_ns"] = time.time_ns()
        window = fault_window(label, stage)
        if window is not None:
            position = int((time.monotonic() - row["audio_started_monotonic"]) * 16000)
            row["fault_stopped_audio_sample"] = position
            row["fault_window_samples"] = list(window)
            if not window[0] <= position < window[1]:
                row["fault_injected"] = False
                row["failures"].append("FAULT_STAGE_MISSED")
        row["failure_cause"] = "server_container_stopped"
        row["fallback_deadline_utc_ns"] = row["fault_requested_utc_ns"] + int(
            deadline_seconds * 1e9
        )

    try:
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

            async with asyncio.timeout(len(pcm) / 32000 + deadline_seconds + 15):
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
                            "data": event.get("data", {}),
                            "t_utc_ns": time.time_ns(),
                            "source": "ha",
                            "quality": "measured",
                        }
                    )
                    if kind == "run-start":
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
                            event.get("data", {}).get("code", "pipeline_error")
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
            if not row["action_trace_complete"]:
                row["failures"].append("MISSING_COMPLETE_OPERATION_RECORD")
            if not isinstance(row["recognized_transcript"], str):
                row["failures"].append("MISSING_TRANSCRIPT")
            if not isinstance(row["evaluated_intent"], dict):
                row["failures"].append("MISSING_SELECTED_INTENT")
            if not row["turn_id"]:
                row["failures"].append("MISSING_TURN_ID")
    except (
        ValueError,
        KeyError,
        TypeError,
        TimeoutError,
        aiohttp.ClientError,
    ) as error:
        row["failures"].append(type(error).__name__)
    finally:
        if sender is not None:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        try:
            if injection is not None:
                try:
                    await injection
                except ValueError, TimeoutError:
                    row["failures"].append("FAULT_INJECTION_FAILED")
        finally:
            await server.restore()
    row["acceptable"] = acceptable(row)
    if not row["acceptable"] and not row["failures"]:
        row["failures"].append("INCORRECT_TRANSCRIPT_OR_OPERATIONS")
    return row


@asynccontextmanager
async def temporary_pipeline(
    command: Command, result: Row, source_id: str
) -> AsyncIterator[tuple[str, str, str]]:
    async def snapshot() -> Row:
        pipelines = await command("assist_pipeline/pipeline/list")
        states = await command("get_states")
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

    result["bench_before"] = await snapshot()
    result["bench_restored"] = False
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
        fields.update(
            name="assist-live f11-smoke",
            conversation_engine="conversation.assist_live_short",
            prefer_local_intents=False,
        )
        created = await command("assist_pipeline/pipeline/create", **fields)
        result["temporary_pipeline"] = created["id"]
        yield created["id"], device_id, sensors[0]
    finally:
        if created is not None:
            await command("assist_pipeline/pipeline/delete", pipeline_id=created["id"])
        result["bench_after"] = await snapshot()
        result["bench_restored"] = result["bench_before"] == result["bench_after"]
        if not result["bench_restored"]:
            raise ValueError("pipeline list or satellite select changed")


async def run(args: argparse.Namespace) -> Row:
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("simulated results must stay external to the repository")
    labels = load_corpus(args.labels)
    if args.native_matrix:
        validate_matrix_document(args.native_matrix.read_text())
    result: Row = {
        "schema_version": 2,
        "scope": "simulated",
        "trials": [],
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
                fault_sample(label, stage)
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
    try:
        await server.restore()
        async with aiohttp.ClientSession() as session:

            async def trials(
                pipeline: str, device_id: str | None = None, sensor: str | None = None
            ) -> None:
                for stage in args.stages.split(","):
                    for index, (label, pcm) in enumerate(plans[stage]):
                        row = await pipeline_trial(
                            session,
                            args.token_file,
                            pipeline,
                            label,
                            pcm,
                            stage,
                            server,
                            args.fallback_deadline,
                            device_id=device_id,
                            turn_sensor=sensor,
                        )
                        result["trials"].append(row)
                        result["stage_status"][stage] = {
                            "status": "RECORDED",
                            "trials": index + 1,
                        }
                        write_result(args.output, result)
                        print(
                            f"{'PASS' if row['acceptable'] else 'FAIL'} simulated "
                            f"{stage} {index + 1}: " + ",".join(row["failures"]),
                            flush=True,
                        )

            if args.temporary_agent_pipeline:
                async with await connect(session, args.token_file) as control:
                    counter = 0

                    async def command(kind: str, **fields: Any) -> Any:
                        nonlocal counter
                        counter += 1
                        async with asyncio.timeout(30):
                            await control.send_json(
                                {"id": counter, "type": kind, **fields}
                            )
                            response = await control.receive_json()
                        if (
                            response.get("id") != counter
                            or response.get("success") is not True
                        ):
                            raise ValueError("isolated bench control command failed")
                        return response.get("result")

                    async with temporary_pipeline(command, result, args.pipeline) as (
                        pipeline,
                        device_id,
                        sensor,
                    ):
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
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--pipeline")
    parser.add_argument("--labels", type=Path, default=Path("tests/data/labels.jsonl"))
    parser.add_argument("--audio-root", type=Path)
    parser.add_argument("--fallback-deadline", type=float, default=30)
    parser.add_argument("--native-matrix", type=Path)
    parser.add_argument("--temporary-agent-pipeline", action="store_true")
    args = parser.parse_args(argv)
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
        not args.simulate
        or not args.token_file
        or not args.pipeline
        or not args.audio_root
    ):
        parser.error(
            "use --simulate with --token-file, --pipeline, and --audio-root; "
            "hardware is not implemented"
        )
    if (
        args.each <= 0
        or not args.stages
        or len(args.stages.split(",")) != len(set(args.stages.split(",")))
        or not set(args.stages.split(",")) <= set(STAGES)
        or not math.isfinite(args.fallback_deadline)
        or args.fallback_deadline <= 0
    ):
        parser.error(
            "positive counts and deadline, and unique known stages are required"
        )
    try:
        result = asyncio.run(run(args))
        passed = sum(acceptable(row) for row in result["trials"])
        print(
            f"NOTE simulated faults {passed}/{len(result['trials'])}; "
            "hardware acceptance NOT RUN"
        )
        return int(
            result.get("preflight_failed") is True
            or args.check
            and (not fault_gate(result)["passed"] or bool(result.get("native_errors")))
        )
    except OSError, ValueError, aiohttp.ClientError, TimeoutError:
        print("FAIL isolated fault run; inspect the private trial records")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
