import argparse
import asyncio
import cmath
import hashlib
import json
import math
import os
import re
import statistics
import struct
import time
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import aiohttp

from tools import fault_runner as runner
from tools.device_replay import BRIDGE_OPERATION_MAP
from tools.measurement import LOSS_FLAGS, capture_loss, loss_window
from tools.probe_pipelines import Command

Row = dict[str, Any]
CORRELATION_MINIMUM = 0.18
PEAK_RATIO_MINIMUM = 1.15
WINDOW_TOLERANCE_SAMPLES = 640
RECORDINGS = PurePosixPath("/config/pipeline_recordings")


class SatelliteNotIdleError(TimeoutError):
    pass


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (
            1,
            2,
            16000,
        ):
            raise ValueError("audio requires mono PCM16 at 16000 Hz")
        pcm = wav.readframes(wav.getnframes())
    if not pcm:
        raise ValueError("empty hardware audio")
    return pcm


def fault_interval(
    sent: object, last: object, offset: object
) -> tuple[int, int] | None:
    if not (
        type(sent) is int
        and type(last) is int
        and type(offset) is int
        and 0 <= last <= sent
    ):
        return None
    return sent - last - offset, sent - offset


def injection_status(row: Row) -> str:
    if row.get("hardware_invalid_reason") in {
        "no_wake",
        "early_end",
        "fault_outside_window",
    }:
        return "invalid_injection"
    correlation = row.get("correlation")
    if not isinstance(correlation, dict) or correlation.get("complete") is not True:
        return "unavailable"
    if row.get("stage") not in ("speech", "pause"):
        return "verified"
    interval = fault_interval(
        row.get("server_audio_samples"),
        row.get("server_audio_last_chunk_samples"),
        correlation.get("offset_samples"),
    )
    try:
        window = runner.fault_window(row.get("label", {}), row["stage"])
    except ValueError:
        return "unavailable"
    if interval is None or window is None:
        return "unavailable"
    uncertainty = correlation.get("uncertainty_samples", 0)
    if type(uncertainty) is not int or uncertainty < 0:
        return "unavailable"
    return (
        "verified"
        if window[0] <= interval[0] - uncertainty
        and interval[1] + uncertainty < window[1]
        else "invalid_injection"
    )


def tts_cause(row: Row) -> tuple[bool, str | None]:
    turns = row.get("fallback_turns")
    if not isinstance(turns, list):
        return False, None
    records = [
        record
        for record in turns
        if isinstance(record, dict) and record.get("turn_id") == row.get("turn_id")
    ]
    if len(records) != 1 or not isinstance(records[0].get("destinations"), list):
        return False, None
    destinations = records[0]["destinations"]
    if any(not isinstance(part, dict) for part in destinations):
        return False, None
    causes = [
        part.get("fallback_reason")
        for part in destinations
        if part.get("part") == "tts_fallback"
    ]
    if not causes:
        return True, None
    if len(causes) != 1 or not isinstance(causes[0], str) or not causes[0]:
        return False, None
    return True, causes[0]


def recorded_reason_matches(row: Row) -> bool:
    evidence, reason = runner.stt_reason_for_turn(row)
    tts_evidence, tts_reason = tts_cause(row)
    if (
        not evidence
        or not tts_evidence
        or reason != row.get("failure_cause")
        or tts_reason != row.get("tts_failure_cause")
    ):
        return False
    if row.get("stage") in ("after-final", "baseline"):
        return reason is None
    stage = row.get("stage")
    return isinstance(stage, str) and reason in runner.EXPECTED_REASONS.get(
        stage, set()
    )


def fft(values: list[complex], *, inverse: bool = False) -> list[complex]:
    result = values.copy()
    size = len(result)
    reverse = 0
    for index in range(1, size):
        bit = size >> 1
        while reverse & bit:
            reverse ^= bit
            bit >>= 1
        reverse ^= bit
        if index < reverse:
            result[index], result[reverse] = result[reverse], result[index]
    width = 2
    while width <= size:
        root = cmath.exp((2j if inverse else -2j) * math.pi / width)
        half = width // 2
        for begin in range(0, size, width):
            factor = 1 + 0j
            for index in range(begin, begin + half):
                even, odd = result[index], result[index + half] * factor
                result[index], result[index + half] = even + odd, even - odd
                factor *= root
        width *= 2
    return [value / size for value in result] if inverse else result


def peak(recording: list[int], reference: list[int], overlap: float) -> Row:
    if not recording or not reference:
        return {}
    size = 1 << (len(recording) + len(reference) - 2).bit_length()
    left = fft([complex(n) for n in recording] + [0j] * (size - len(recording)))
    right = fft(
        [complex(n) for n in reversed(reference)] + [0j] * (size - len(reference))
    )
    cross = fft([a * b for a, b in zip(left, right, strict=True)], inverse=True)
    energies = []
    for samples in (recording, reference):
        sums = [0]
        for sample in samples:
            sums.append(sums[-1] + sample * sample)
        energies.append(sums)
    scores = []
    for lag in range(-len(reference) + 1, len(recording)):
        lo, hi = max(0, lag), min(len(recording), lag + len(reference))
        if hi - lo < max(64, len(reference) * overlap):
            continue
        power = (energies[0][hi] - energies[0][lo]) * (
            energies[1][hi - lag] - energies[1][lo - lag]
        )
        score = (
            abs(cross[lag + len(reference) - 1].real) / math.sqrt(power) if power else 0
        )
        scores.append((score, lag))
    if not scores:
        return {}
    score, offset = max(scores)
    second = max(
        (
            value
            for value, lag in scores
            if abs(lag - offset) > WINDOW_TOLERANCE_SAMPLES
        ),
        default=0,
    )
    return {
        "offset_samples": offset,
        "correlation": score,
        "second_peak_ratio": score / max(second, 1e-12),
    }


def correlate(pcm: bytes, source: bytes) -> Row:
    def difference(data: bytes) -> list[int]:
        samples = [n[0] for n in struct.iter_unpack("<h", data)]
        return (
            [0, *(b - a for a, b in zip(samples, samples[1:], strict=False))]
            if samples
            else []
        )

    recording, reference = difference(pcm), difference(source)
    result = peak(recording, reference, 0.55)
    result.update(
        recording_samples=len(recording),
        reference_samples=len(reference),
        complete=False,
    )
    offset = result.get("offset_samples")
    if (
        offset is None
        or result.get("correlation", 0) < CORRELATION_MINIMUM
        or result.get("second_peak_ratio", 0) < PEAK_RATIO_MINIMUM
    ):
        result["offset_samples"] = None
        return result
    width = min(9600, len(reference) // 6)
    windows = []
    for third in range(3):
        begin, end = len(reference) * third // 3, len(reference) * (third + 1) // 3
        starts = range(begin, max(begin + 1, end - width + 1), 160)
        start = max(
            starts,
            key=lambda n: sum(value * value for value in reference[n : n + width]),
        )
        window = peak(recording, reference[start : start + width], 0.95)
        window["reference_start_sample"] = start
        candidate = window.get("offset_samples")
        window["clip_offset_samples"] = (
            candidate - start if candidate is not None else None
        )
        windows.append(window)
    result["windows"] = windows
    agreed = [
        window["clip_offset_samples"]
        for window in windows
        if window.get("clip_offset_samples") is not None
        and window.get("correlation", 0) >= CORRELATION_MINIMUM
        and window.get("second_peak_ratio", 0) >= PEAK_RATIO_MINIMUM
        and abs(window["clip_offset_samples"] - offset) <= WINDOW_TOLERANCE_SAMPLES
    ]
    if len(agreed) < 2:
        result["offset_samples"] = None
        return result
    result["uncertainty_samples"] = max(abs(n - offset) for n in agreed)
    result["complete"] = (
        len(agreed) == 3 and 0 <= offset and offset + len(reference) <= len(recording)
    )
    return result


def event_ns(event: Row) -> int:
    timestamp = datetime.fromisoformat(event["timestamp"])
    if timestamp.tzinfo is None:
        raise ValueError("pipeline event lacks timezone")
    return int(timestamp.timestamp() * 1_000_000_000)


class Hardware:
    def __init__(self, args: argparse.Namespace) -> None:
        self.satellite: str = args.satellite
        self.sink: str = args.playback_sink
        self.container: str = args.ha_container
        self.directory: Path = args.turn_audio_archive.resolve()
        if self.directory.is_relative_to(runner.ROOT):
            raise ValueError("hardware archive must be outside the repository")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.container):
            raise ValueError("invalid HA container")
        if not self.satellite.startswith("assist_satellite."):
            raise ValueError("satellite entity required")
        self.wake = read_pcm(args.wake_audio)
        self.device_id = ""
        self.select_entity = ""
        self.mute_entity = ""
        self.pipeline_name = ""
        self.delay_samples: list[float] = []

    async def preflight(self, command: Command) -> None:
        registry = await command("config/entity_registry/list")
        devices = {
            row["device_id"]
            for row in registry
            if row["entity_id"] == self.satellite and row.get("device_id")
        }
        if len(devices) != 1:
            raise ValueError("unique satellite device unavailable")
        self.device_id = devices.pop()
        selects = [
            row["pipeline_entity"]
            for row in await command("assist_pipeline/device/list")
            if row["device_id"] == self.device_id and row.get("pipeline_entity")
        ]
        mutes = [
            row["entity_id"]
            for row in registry
            if row.get("device_id") == self.device_id
            and row["entity_id"].startswith("switch.")
            and (
                row.get("translation_key") in ("microphone", "mute")
                or row.get("original_name", "").lower() in ("mute", "microphone mute")
            )
        ]
        if len(selects) != 1 or len(mutes) != 1:
            raise ValueError(
                "unique satellite pipeline select and microphone mute required"
            )
        self.select_entity, self.mute_entity = selects[0], mutes[0]
        await self.ready(command)

    async def ready(self, command: Command) -> Row:
        deadline = time.monotonic() + 30
        try:
            async with asyncio.timeout(30):
                while True:
                    states = {
                        row["entity_id"]: row for row in await command("get_states")
                    }
                    if states.get(self.mute_entity, {}).get("state") != "off":
                        raise ValueError("satellite microphone muted or unknown")
                    if states.get(self.satellite, {}).get("state") == "idle":
                        return states
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    await asyncio.sleep(min(0.25, remaining))
        except TimeoutError as error:
            raise SatelliteNotIdleError("satellite_not_idle") from error

    async def select(self, command: Command, option: str) -> None:
        await command(
            "call_service",
            domain="select",
            service="select_option",
            target={"entity_id": self.select_entity},
            service_data={"option": option},
        )
        for _ in range(30):
            states = {row["entity_id"]: row for row in await command("get_states")}
            if states.get(self.select_entity, {}).get("state") == option:
                return
            await asyncio.sleep(0.2)
        raise runner.TemporaryPipelineError("SATELLITE_PIPELINE_READ_BACK_FAILED")

    @asynccontextmanager
    async def selected(
        self, command: Command, pipeline: str, result: Row
    ) -> AsyncIterator[None]:
        states = await self.ready(command)
        previous = states[self.select_entity]["state"]
        if previous in ("unknown", "unavailable"):
            raise ValueError("original satellite pipeline unavailable")
        self.pipeline_name = (
            await command("assist_pipeline/pipeline/get", pipeline_id=pipeline)
        )["name"]
        result["satellite_pipeline_before"] = previous
        result["satellite_pipeline_restored"] = False
        try:
            await self.select(command, self.pipeline_name)
            yield
        finally:

            async def restore() -> None:
                await self.select(command, previous)
                result["satellite_pipeline_restored"] = True

            task = asyncio.create_task(restore())
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            task.result()
            if cancelled:
                raise asyncio.CancelledError

    async def files(self, server: runner.TestServer) -> set[str]:
        root = RECORDINGS / self.device_id / self.pipeline_name
        code = (
            "import json,os,sys; "
            "print(json.dumps([os.path.join(r,n) for r,ds,ns in os.walk(sys.argv[1]) "
            "for n in ns if n.startswith('01_stt-') and n.endswith('.wav')]))"
        )
        return set(
            json.loads(
                await server.docker(
                    "exec", self.container, "python3", "-c", code, str(root)
                )
            )
        )

    async def archive(
        self, server: runner.TestServer, source: str, turn: str, run: str
    ) -> Row:
        if (
            not PurePosixPath(source).is_relative_to(RECORDINGS)
            or ".." in PurePosixPath(source).parts
        ):
            raise ValueError("invalid recording path")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        target = self.directory / (uuid4().hex + "-turn.wav")
        code = (
            "import hashlib,sys; "
            "print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())"
        )
        expected = await server.docker(
            "exec", self.container, "python3", "-c", code, source
        )
        with target.open("xb"):
            target.chmod(0o600)
        try:
            await server.docker("cp", self.container + ":" + source, str(target))
        finally:
            target.chmod(0o600)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        after = await server.docker(
            "exec", self.container, "python3", "-c", code, source
        )
        if digest != expected or digest != after:
            raise ValueError("turn audio SHA-256 mismatch; all copies retained")
        return {
            "path": str(target),
            "sha256": digest,
            "source": source,
            "turn_id": turn,
            "pipeline_run_id": run,
        }

    async def baselines(
        self,
        session: aiohttp.ClientSession,
        token_file: Path,
        pipeline: str,
        label: Row,
        source_pcm: bytes,
        engines: Row,
        server: runner.TestServer,
        row: Row,
        sensor: str,
    ) -> None:
        record = row.get("turn_audio")
        if not record:
            return
        path = Path(record["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("archived turn audio SHA-256 mismatch")
        turn_pcm = read_pcm(path)
        turn_label = {**label, "audio_sha256": record["sha256"]}
        row["source_audio_sha256"] = label["audio_sha256"]
        row["audio_sha256"] = record["sha256"]
        row["baselines"] = await runner.collect_baselines(
            session,
            token_file,
            pipeline,
            turn_label,
            turn_pcm,
            engines,
            server,
            device_id=self.device_id,
            turn_sensor=sensor,
        )

    def calibrate(self, row: Row) -> None:
        correlation = row.get("correlation") or {}
        offset = correlation.get("offset_samples")
        if type(offset) is not int:
            return
        interval = fault_interval(
            row.get("server_audio_samples"),
            row.get("server_audio_last_chunk_samples"),
            offset,
        )
        if (
            row.get("stage") in ("speech", "pause")
            and interval is not None
            and row.get("fault_stopped_monotonic") is not None
        ):
            elapsed = row["fault_stopped_monotonic"] - row["audio_started_monotonic"]
            delay = elapsed - len(self.wake) / 32000 - statistics.mean(interval) / 16000
        else:
            starts = [event for event in row["events"] if event["stage"] == "stt-start"]
            if len(starts) != 1:
                return
            delay = (
                (starts[0]["t_utc_ns"] - row["playback_launch_utc_ns"]) / 1e9
                + offset / 16000
                - len(self.wake) / 32000
            )
        if math.isfinite(delay):
            self.delay_samples.append(delay)

    async def trial(
        self,
        session: aiohttp.ClientSession,
        token_file: Path,
        pipeline: str,
        label: Row,
        pcm: bytes,
        stage: str,
        server: runner.TestServer,
        *,
        stop_latency_seconds: float,
        stop_margin_seconds: float,
        sensor: str,
    ) -> Row:
        row: Row = {
            "schema_version": 2,
            "scope": "hardware",
            "mode": "smoke",
            "phase": "p1",
            "profile": "safe",
            "frozen": False,
            "split": label.get("split"),
            "run_id": "hardware-faults",
            "baseline_config_id": pipeline,
            "stage": stage,
            "label": label,
            "clip_id": label["id"],
            "audio_sha256": label["audio_sha256"],
            "turn_id": None,
            "pipeline_request_id": None,
            "events": [],
            "agent_records": [],
            "action_trace": [],
            "action_trace_complete": False,
            "recognized_transcript": None,
            "evaluated_intent": None,
            "failures": [],
            "failure_messages": [],
            "fault_injected": False,
            "injected_fault": None,
            "failure_cause": None,
            "correlation": None,
            "hardware_invalid_reason": None,
            "playback_delay_samples_seconds": list(self.delay_samples),
        }
        playback = None
        injection = None
        listener = None
        before = await runner.fallback_snapshot(session, token_file, pipeline)
        row["fallback_reasons_before"] = before["fallback_reasons"] if before else None
        loss_start = await capture_loss(
            None, satellites=[self.device_id], integration_data=before
        )
        row["loss_counters"] = {"start": loss_start, "end": None}
        try:
            async with runner.control_command(session, token_file) as command:
                states = await self.ready(command)
                previous = states[sensor]["attributes"].get("turn_id")
                known = {
                    entry["pipeline_run_id"]
                    for entry in (
                        await command(
                            "assist_pipeline/pipeline_debug/list", pipeline_id=pipeline
                        )
                    )["pipeline_runs"]
                }
                files_before = await self.files(server)
                row["recordings_before"] = sorted(files_before)
                row["pipeline_runs_before"] = sorted(known)
                observed_turns: set[str] = set()
                async with await runner.connect(session, token_file) as ws:
                    await ws.send_json({"id": 1, "type": "subscribe_events"})
                    if (await ws.receive_json()).get("success") is not True:
                        raise ValueError("hardware event subscription failed")

                    async def listen() -> None:
                        while True:
                            message = await ws.receive_json()
                            event = message.get("event", {})
                            data = event.get("data", {})
                            if event.get("event_type") == "assist_live_test_record":
                                row["agent_records"].append(data)
                            if (
                                event.get("event_type") == "state_changed"
                                and data.get("entity_id") == sensor
                            ):
                                attributes = (data.get("new_state") or {}).get(
                                    "attributes", {}
                                )
                                if attributes.get(
                                    "turn_type"
                                ) == "voice" and attributes.get("turn_id") not in (
                                    None,
                                    previous,
                                ):
                                    observed_turns.add(attributes["turn_id"])

                    listener = asyncio.create_task(listen())

                    async def inject() -> None:
                        started = time.monotonic()
                        row["fault_requested_utc_ns"] = time.time_ns()
                        await server.stop()
                        row.update(
                            measured_stop_latency_seconds=time.monotonic() - started,
                            fault_stopped_monotonic=time.monotonic(),
                            fault_stopped_utc_ns=time.time_ns(),
                            fault_injected=True,
                            injected_fault="server_container_stopped",
                        )

                    planned = runner.fault_sample(
                        label,
                        stage,
                        stop_latency_seconds=stop_latency_seconds,
                        stop_margin_seconds=stop_margin_seconds,
                    )
                    row["fault_planned_audio_sample"] = planned
                    window = runner.fault_window(label, stage)
                    row["fault_window_samples"] = list(window) if window else None
                    if stage in ("speech", "pause") and not self.delay_samples:
                        raise ValueError("hardware playback calibration unavailable")
                    if stage == "before":
                        await inject()
                    self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                    self.directory.chmod(0o700)
                    composite = self.directory / (uuid4().hex + "-playback.wav")
                    with composite.open("xb") as output:
                        os.chmod(composite, 0o600)
                        with wave.open(output, "wb") as wav:
                            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                            wav.writeframes(self.wake + pcm)
                    row["playback_audio"] = {
                        "path": str(composite),
                        "sha256": hashlib.sha256(composite.read_bytes()).hexdigest(),
                        "wake_samples": len(self.wake) // 2,
                        "gap_samples": 0,
                    }
                    while True:
                        await self.ready(command)
                        await asyncio.sleep(6.2)
                        states = {
                            row["entity_id"]: row for row in await command("get_states")
                        }
                        if states.get(self.mute_entity, {}).get("state") != "off":
                            raise ValueError("satellite microphone muted or unknown")
                        if states.get(self.satellite, {}).get("state") == "idle":
                            break
                    launched = time.monotonic()
                    row.update(
                        audio_started_monotonic=launched,
                        playback_launch_utc_ns=time.time_ns(),
                    )
                    playback = await asyncio.create_subprocess_exec(
                        "pw-play",
                        "--target",
                        self.sink,
                        "--latency",
                        "20ms",
                        str(composite),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    if planned is not None:
                        delay = (
                            len(self.wake) / 32000
                            + planned / 16000
                            + statistics.median(self.delay_samples)
                        )
                        row["fault_schedule_seconds"] = delay

                        async def scheduled() -> None:
                            await asyncio.sleep(
                                max(0, launched + delay - time.monotonic())
                            )
                            await inject()

                        injection = asyncio.create_task(scheduled())
                    fresh: set[str] = set()
                    async with asyncio.timeout(len(self.wake + pcm) / 32000 + 35):
                        while True:
                            listed = await command(
                                "assist_pipeline/pipeline_debug/list",
                                pipeline_id=pipeline,
                            )
                            fresh |= {
                                entry["pipeline_run_id"]
                                for entry in listed["pipeline_runs"]
                            } - known
                            if len(fresh) > 1:
                                raise ValueError("ambiguous hardware pipeline runs")
                            if fresh:
                                run = next(iter(fresh))
                                events = (
                                    await command(
                                        "assist_pipeline/pipeline_debug/get",
                                        pipeline_id=pipeline,
                                        pipeline_run_id=run,
                                    )
                                )["events"]
                                starts = [
                                    event
                                    for event in events
                                    if event["type"] == "run-start"
                                ]
                                if (
                                    len(starts) != 1
                                    or starts[0]["data"].get("satellite_id")
                                    != self.satellite
                                ):
                                    raise ValueError("hardware run satellite mismatch")
                                row["pipeline_run_id"] = run
                                row["pipeline_request_id"] = 1
                                row["events"] = [
                                    {
                                        "stage": event["type"],
                                        "data": runner.mapping(event.get("data")),
                                        "t_utc_ns": event_ns(event),
                                        "source": "ha",
                                        "request_id": 1,
                                    }
                                    for event in events
                                ]
                                row["failure_messages"] = [
                                    runner.failure_message(
                                        ValueError(str(event["data"]))
                                    )
                                    for event in row["events"]
                                    if event["stage"] == "error"
                                ]
                                row["fallback_deadline_utc_ns"] = (
                                    event_ns(starts[0]) + runner.FALLBACK_LIMIT_NS
                                )
                                finals = [
                                    event
                                    for event in events
                                    if event["type"] == "stt-end"
                                ]
                                if finals:
                                    row["recognized_transcript"] = finals[0]["data"][
                                        "stt_output"
                                    ]["text"]
                                    if stage == "after-final" and injection is None:
                                        row["final_observed_utc_ns"] = time.time_ns()
                                        injection = asyncio.create_task(inject())
                                if any(event["type"] == "run-end" for event in events):
                                    ends = [
                                        event
                                        for event in events
                                        if event["type"] in ("intent-end", "error")
                                    ]
                                    row["completed_utc_ns"] = (
                                        event_ns(ends[-1]) if ends else None
                                    )
                                    break
                            if not fresh and playback.returncode is not None:
                                await asyncio.sleep(2)
                                listed = await command(
                                    "assist_pipeline/pipeline_debug/list",
                                    pipeline_id=pipeline,
                                )
                                if not (
                                    {
                                        entry["pipeline_run_id"]
                                        for entry in listed["pipeline_runs"]
                                    }
                                    - known
                                ):
                                    row["hardware_invalid_reason"] = "no_wake"
                                    break
                            await asyncio.sleep(0.1)
                    states = {
                        state["entity_id"]: state
                        for state in await command("get_states")
                    }
                    attributes = states[sensor]["attributes"]
                    if attributes.get("turn_type") == "voice" and attributes.get(
                        "turn_id"
                    ) not in (None, previous):
                        observed_turns.add(attributes["turn_id"])
                    if row["hardware_invalid_reason"] != "no_wake":
                        if len(observed_turns) != 1:
                            raise ValueError("unique hardware turn ID unavailable")
                        row["turn_id"] = observed_turns.pop()
                        row["turn_id_source"] = sensor
                        new_files = await self.files(server) - files_before
                        row["recordings_after"] = sorted(new_files | files_before)
                        if len(new_files) != 1:
                            raise ValueError("unique hardware turn WAV unavailable")
                        row["turn_audio"] = await self.archive(
                            server,
                            new_files.pop(),
                            row["turn_id"],
                            row["pipeline_run_id"],
                        )
                        turn_pcm = read_pcm(Path(row["turn_audio"]["path"]))
                        row["correlation"] = await asyncio.to_thread(
                            correlate, turn_pcm, pcm
                        )
                        correlation = row["correlation"]
                        offset = correlation.get("offset_samples")
                        if len(turn_pcm) < len(pcm) or (
                            offset is not None
                            and (
                                offset < 0
                                or offset + len(pcm) // 2 > len(turn_pcm) // 2
                            )
                        ):
                            row["hardware_invalid_reason"] = "early_end"
                    records = [
                        record
                        for record in row["agent_records"]
                        if record.get("turn_id") == row["turn_id"]
                        and record.get("device_id") == self.device_id
                    ]
                    if len(records) == 1:
                        record = records[0]
                        if (
                            record.get("recognized_transcript")
                            != row["recognized_transcript"]
                        ):
                            raise ValueError("hardware transcript link mismatch")
                        row.update(
                            evaluated_intent=record.get("selected_intent"),
                            action_trace_complete=record.get("completed") is True,
                        )
                        row["action_trace"] = [
                            {
                                target: operation[source]
                                for source, target in BRIDGE_OPERATION_MAP.items()
                            }
                            for operation in record.get("operations", [])
                        ]
                    if runner.no_text_error(row):
                        row.update(
                            action_trace=[], action_trace_complete=True, operations=[]
                        )
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            TimeoutError,
            aiohttp.ClientError,
        ) as error:
            row["failures"].append(type(error).__name__)
            row["failure_messages"].append(runner.failure_message(error))
            if isinstance(error, SatelliteNotIdleError):
                row["unavailable_reason"] = "satellite_not_idle"
        finally:
            try:
                if playback is not None and playback.returncode is None:
                    playback.terminate()
                    await playback.wait()
                if playback is not None and playback.returncode not in (0, -15):
                    row["failures"].append("PLAYBACK_FAILED")
                if listener is not None:
                    listener.cancel()
                    await asyncio.gather(listener, return_exceptions=True)
                if injection is not None:
                    if not injection.done() and "fault_requested_utc_ns" not in row:
                        injection.cancel()
                    outcome = (await asyncio.gather(injection, return_exceptions=True))[
                        0
                    ]
                    if isinstance(outcome, BaseException):
                        row["failures"].append("FAULT_INJECTION_FAILED")
                        row["failure_messages"].append(runner.failure_message(outcome))
                after = await runner.fallback_snapshot(session, token_file, pipeline)
                loss_end = await capture_loss(
                    None, satellites=[self.device_id], integration_data=after
                )
                row["loss_counters"] = loss_window(loss_start, loss_end)
                for flag in LOSS_FLAGS:
                    row[flag] = row["loss_counters"][flag]
                row["fallback_reasons_after"] = (
                    after["fallback_reasons"] if after else None
                )
                row["fallback_turns"] = after["turns"] if after else None
                row["fallback_reasons_delta"] = runner.reason_delta(
                    row["fallback_reasons_before"], row["fallback_reasons_after"]
                )
                _, row["failure_cause"] = runner.stt_reason_for_turn(row)
                _, row["tts_failure_cause"] = tts_cause(row)
                turns = [
                    record
                    for record in (row["fallback_turns"] or [])
                    if record.get("turn_id") == row["turn_id"]
                ]
                if len(turns) == 1:
                    for key in (
                        "server_audio_samples",
                        "server_audio_last_chunk_samples",
                    ):
                        row[key] = turns[0].get(key)
                offset = (row.get("correlation") or {}).get("offset_samples")
                interval = fault_interval(
                    row.get("server_audio_samples"),
                    row.get("server_audio_last_chunk_samples"),
                    offset,
                )
                row["fault_recording_interval_samples"] = (
                    list(interval) if interval else None
                )
                if interval is not None:
                    row["fault_stopped_audio_sample"] = interval[1]
                self.calibrate(row)
                if (
                    row["hardware_invalid_reason"] is None
                    and injection_status(row) == "invalid_injection"
                ):
                    row["hardware_invalid_reason"] = "fault_outside_window"
            finally:
                await server.restore()
        if injection_status(row) != "verified":
            runner.complete_failure_messages(row)
        return row
