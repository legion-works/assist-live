import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import shutil
import struct
import sys
import time
import wave
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeGuard, cast

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assist_live_client import AssistLiveClient
from assist_live_client.models import TimingEvent

from tools.corpus import (
    boundary_review_reasons,
    check_audio,
    load_corpus,
    resolve_audio,
    text,
)
from tools.test_environment import (
    validate_intent_action_evidence,
    validate_run_evidence,
)

Row = dict[str, Any]
DEVICE_STAGES = ("wake_detected", "mic_open", "playback_start", "playback_end")
BRIDGE_FIELDS = (
    "turn_id",
    "run_id",
    "baseline_configuration_id",
    "completed",
    "operations",
)
BRIDGE_OPERATION_MAP = {
    "service": "service",
    "target": "virtual_target",
    "arguments": "arguments",
    "t_monotonic_ns": "monotonic_ns",
}
ROOT = Path(__file__).resolve().parents[1]


class ReplayError(ValueError):
    pass


def finite(value: object) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def sample_errors(row: Row, *, acceptance: bool = True) -> list[str]:
    errors = []
    if row.get("schema_version") != 2:
        errors.append("SCHEMA_VERSION")
    for key in (
        "clip_id",
        "run_id",
        "baseline_config_id",
        "turn_id",
        "calibration_revision",
        "recognized_transcript",
        "reply_text",
    ):
        if not text(row.get(key)):
            errors.append("MISSING_" + key.upper())
    if row.get("phase") not in ("p0", "p1"):
        errors.append("PHASE")
    if row.get("mode") not in ("smoke", "acceptance") or (
        acceptance and row.get("mode") != "acceptance"
    ):
        errors.append("SMOKE_NOT_ACCEPTANCE")
    if row.get("profile") not in (
        ("baseline",) if row.get("phase") == "p0" else ("safe", "fast")
    ):
        errors.append("PROFILE")
    if row.get("split") not in ("held_out", "tuning"):
        errors.append("SPLIT")
    digest = row.get("audio_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        errors.append("AUDIO_SHA256")
    if type(row.get("frozen")) is not bool or (
        acceptance and row.get("frozen") is not True
    ):
        errors.append("BLOCKED corpus not frozen")
    intent = row.get("evaluated_intent")
    if (
        not isinstance(intent, dict)
        or not text(intent.get("name"))
        or not isinstance(intent.get("slots"), dict)
    ):
        errors.append("MISSING_INTENT")
    if row.get("action_trace_complete") is not True:
        errors.append("INCOMPLETE_OPERATION_LOG")
    actions = row.get("action_trace")
    if not isinstance(actions, list):
        errors.append("MISSING_OPERATION_LOG")
    else:
        for action in actions:
            if (
                not isinstance(action, dict)
                or not text(action.get("service"))
                or not text(action.get("virtual_target"))
                or not isinstance(action.get("arguments"), dict)
                or not finite(action.get("monotonic_ns"))
                or action["monotonic_ns"] < 0
            ):
                errors.append("INVALID_OPERATION")
    reply = row.get("reply_text")
    if (
        not isinstance(reply, str)
        or type(row.get("reply_char_count")) is not int
        or row["reply_char_count"] != len(reply)
    ):
        errors.append("REPLY_CHAR_COUNT")
    elif row.get("reply_group") != ("short" if len(reply) <= 60 else "long"):
        errors.append("REPLY_GROUP")
    if row.get("phase") == "p1" and not text(row.get("baseline_transcript")):
        errors.append("MISSING_BASELINE_TRANSCRIPT")
    events = row.get("events")
    if not isinstance(events, list):
        events = []
        errors.append("MISSING_EVENTS")
    valid_events = []
    for event in events:
        try:
            TimingEvent(**event)
        except TypeError, ValueError, AttributeError:
            errors.append("INVALID_EVENT")
            continue
        valid_events.append(event)
        if event["turn_id"] != row.get("turn_id") or event["stage"] == "unmatched":
            errors.append("UNMATCHED_TURN")
    for stage in DEVICE_STAGES:
        found = [
            event
            for event in valid_events
            if event["stage"] == stage
            and event["source"] == "device"
            and event.get("t_device_ms") is not None
            and event.get("seq") is not None
        ]
        if len(found) != 1:
            errors.append(
                "DUPLICATE_WAKE"
                if stage == "wake_detected" and len(found) > 1
                else "MISSING_DEVICE_" + stage.upper()
            )
        elif row.get(stage) != found[0]:
            errors.append("DEVICE_BOUNDARY_MISMATCH")
    host = row.get("host_timing")
    if not isinstance(host, dict) or any(
        not finite(host.get(key))
        for key in (
            "mark_start_utc_ns",
            "mark_start_monotonic_ns",
            "command_play_requested_utc_ns",
        )
    ):
        errors.append("MISSING_HOST_TIMING")
    alignment = row.get("alignment")
    if (
        not isinstance(alignment, dict)
        or alignment.get("quality") != "estimate"
        or not text(alignment.get("method"))
        or any(
            not finite(alignment.get(key)) or alignment[key] < 0
            for key in (
                "uncertainty_ms",
                "last_voice_utc_ns",
                "reply_start_utc_ns",
                "reply_end_utc_ns",
            )
        )
    ):
        errors.append("MISSING_ALIGNMENT")
    elif (
        not alignment["last_voice_utc_ns"]
        < alignment["reply_start_utc_ns"]
        <= alignment["reply_end_utc_ns"]
    ):
        errors.append("ALIGNMENT_ORDER")
    return errors


def validate_sample(row: Row, *, acceptance: bool = True) -> bool:
    return not sample_errors(row, acceptance=acceptance)


def frozen_corpus(labels: Path, manifest: Path, rows: list[Row]) -> bool:
    try:
        entries = dict(
            line.split("  ", 1)[::-1] for line in manifest.read_text().splitlines()
        )
        return entries.get("labels") == hashlib.sha256(
            labels.read_bytes()
        ).hexdigest() and all(
            entries.get("audio/" + row["audio_path"]) == row["audio_sha256"]
            for row in rows
        )
    except OSError, ValueError, KeyError:
        return False


def load_audio(path: Path) -> tuple[list[float], int]:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (
            1,
            2,
            16000,
        ):
            raise ReplayError("AUDIO_FORMAT: expected mono PCM16 at 16000 Hz")
        pcm = wav.readframes(wav.getnframes())
    return [value[0] / 32768 for value in struct.iter_unpack("<h", pcm)], 16000


@dataclass
class Capture:
    samples: list[float]
    rate: int
    start_utc_ns: int
    uncertainty_ms: float


def envelope(samples: list[float], width: int) -> list[float]:
    return [
        math.sqrt(
            sum(x * x for x in samples[i : i + width]) / len(samples[i : i + width])
        )
        for i in range(0, len(samples), width)
    ]


def sustained(values: list[float], threshold: float) -> list[int]:
    active = [value > threshold for value in values]
    return [
        i
        for i in range(len(active))
        if active[i]
        and ((i > 0 and active[i - 1]) or (i + 1 < len(active) and active[i + 1]))
    ]


def align_capture(
    capture: Capture, reference: list[float], last_voice_sample: int
) -> Row:
    if (
        capture.rate != 16000
        or not reference
        or not capture.samples
        or not 0 <= last_voice_sample < len(reference)
        or not finite(capture.uncertainty_ms)
        or capture.uncertainty_ms < 0
    ):
        raise ReplayError("ALIGNMENT_INPUT")
    width = capture.rate // 100
    ref = envelope(reference, width)
    mic = envelope(capture.samples, width)
    energy = sum(x * x for x in ref)
    if energy == 0 or len(mic) < len(ref) or max(mic) < 0.001:
        raise ReplayError("ALIGNMENT_COMMAND_MISSING")
    scores = []
    for lag in range(min(200, len(mic) - len(ref)) + 1):
        segment = mic[lag : lag + len(ref)]
        scale = sum(a * b for a, b in zip(ref, segment, strict=True)) / energy
        residual = sum((b - scale * a) ** 2 for a, b in zip(ref, segment, strict=True))
        scores.append(residual / max(sum(b * b for b in segment), 1e-20))
    lag = min(range(len(scores)), key=scores.__getitem__)
    if scores[lag] > 0.15:
        raise ReplayError("ALIGNMENT_COMMAND_MISSING")
    near = [i for i, score in enumerate(scores) if score <= scores[lag] + 0.002]
    if max(near) - min(near) > 5:
        raise ReplayError("ALIGNMENT_AMBIGUOUS")
    segment = mic[lag : lag + len(ref)]
    noise = sorted(mic)[len(mic) // 10]
    threshold = max(0.001, noise * 4, max(segment) * 0.02)
    command = sustained(segment, threshold)
    if not command:
        raise ReplayError("ALIGNMENT_COMMAND_MISSING")
    command_end = (command[-1] + 1) * width - 1
    reasons = boundary_review_reasons(
        command_end,
        last_voice_sample,
        capture.rate,
        alignment_confidence=1 - scores[lag],
        minimum_word_confidence=1,
        transcript_matches=True,
    )
    if reasons:
        raise ReplayError("ALIGNMENT_BOUNDARY: " + ",".join(reasons))
    reply_offset = lag + command[-1] + 2
    reply = mic[reply_offset:]
    if not reply:
        raise ReplayError("ALIGNMENT_REPLY_MISSING")
    reply_active = sustained(reply, max(0.001, noise * 4, max(reply) * 0.02))
    if not reply_active:
        raise ReplayError("ALIGNMENT_REPLY_MISSING")
    if len(reply) - reply_active[-1] - 1 < 10:
        raise ReplayError("ALIGNMENT_REPLY_TRUNCATED")
    start = (reply_offset + reply_active[0]) * width
    end = min(
        len(capture.samples) - 1, (reply_offset + reply_active[-1] + 1) * width - 1
    )
    uncertainty = capture.uncertainty_ms + 10 + (max(near) - min(near)) * 10
    return {
        "method": "rms-10ms-reference-fit-sustained-20ms",
        "quality": "estimate",
        "uncertainty_ms": uncertainty,
        "command_offset_samples": lag * width,
        "last_voice_sample": lag * width + command_end,
        "reply_start_sample": start,
        "reply_end_sample": end,
        "last_voice_utc_ns": capture.start_utc_ns
        + (lag * width + command_end) * 1_000_000_000 // capture.rate,
        "reply_start_utc_ns": capture.start_utc_ns
        + start * 1_000_000_000 // capture.rate,
        "reply_end_utc_ns": capture.start_utc_ns + end * 1_000_000_000 // capture.rate,
    }


class AudioOutput(Protocol):
    async def play(self, path: Path) -> None: ...


class AudioCapture(Protocol):
    async def start(self) -> None: ...
    async def finish(self) -> Capture: ...
    async def abort(self) -> None: ...


class DeviceEvents(Protocol):
    async def start(self, since_utc_ns: int) -> None: ...
    async def next(self) -> Row: ...
    async def turn(self, turn_id: str) -> Row: ...
    async def export(self, since_utc_ns: int) -> list[Row]: ...
    async def close(self) -> None: ...


class ProfileSelector(Protocol):
    async def select(self, profile: str) -> None: ...


class UnavailableProfileSelector:
    async def select(self, profile: str) -> None:
        raise ReplayError("NOT AVAILABLE: device profile selector")


class AlsaOutput:
    def __init__(self, device: str = "default") -> None:
        self.device = device

    async def play(self, path: Path) -> None:
        if shutil.which("aplay") is None:
            raise ReplayError("AUDIO_OUTPUT_NOT_AVAILABLE: aplay")
        process = await asyncio.create_subprocess_exec(
            "aplay",
            "-q",
            "-D",
            self.device,
            "--",
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(120):
                code = await process.wait()
            if code:
                raise ReplayError("AUDIO_OUTPUT_FAILED")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


class AlsaCapture:
    def __init__(self, device: str = "default") -> None:
        self.device = device
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.pcm = bytearray()
        self.first_byte_ns: int | None = None

    async def start(self) -> None:
        if shutil.which("arecord") is None:
            raise ReplayError("AUDIO_CAPTURE_NOT_AVAILABLE: arecord")
        self.pcm.clear()
        self.first_byte_ns = None
        self.start_ns = time.time_ns()
        self.process = await asyncio.create_subprocess_exec(
            "arecord",
            "-q",
            "-D",
            self.device,
            "-t",
            "raw",
            "-f",
            "S16_LE",
            "-r",
            "16000",
            "-c",
            "1",
            "--",
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def read_pcm() -> None:
            assert self.process is not None and self.process.stdout is not None
            while data := await self.process.stdout.read(3200):
                if self.first_byte_ns is None:
                    self.first_byte_ns = time.time_ns()
                self.pcm.extend(data)
                if len(self.pcm) > 16000 * 2 * 120:
                    raise ReplayError("AUDIO_CAPTURE_LIMIT")

        self.reader = asyncio.create_task(read_pcm())
        await asyncio.sleep(0.1)
        if self.process.returncode is not None:
            await self.abort()
            raise ReplayError("AUDIO_CAPTURE_FAILED")

    async def finish(self) -> Capture:
        assert self.process is not None and self.reader is not None
        stopped = self.process.returncode is not None
        if not stopped:
            self.process.terminate()
        await self.process.wait()
        try:
            await self.reader
            if (
                stopped
                or self.first_byte_ns is None
                or not self.pcm
                or len(self.pcm) % 2
            ):
                raise ReplayError("AUDIO_CAPTURE_FAILED")
            samples = [value[0] / 32768 for value in struct.iter_unpack("<h", self.pcm)]
            # The first read bounds startup delay, not the first acoustic sample.
            uncertainty = (self.first_byte_ns - self.start_ns) / 1e6 + 100
            return Capture(samples, 16000, self.start_ns, uncertainty)
        finally:
            self.pcm.clear()

    async def abort(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        self.pcm.clear()


class ClientEvents:
    def __init__(self, client: AssistLiveClient, satellite: str) -> None:
        self.client, self.satellite = client, satellite
        self.stream: AsyncGenerator[Row] | None = None

    async def start(self, since_utc_ns: int) -> None:
        self.since = since_utc_ns
        self.stream = cast(AsyncGenerator[Row], self.client.events(self.satellite))

    async def next(self) -> Row:
        assert self.stream is not None
        async with asyncio.timeout(120):
            async for envelope in self.stream:
                event = envelope["event"]
                if event["t_utc_ns"] >= self.since:
                    return dict(event)
        raise ReplayError("MISSING_DEVICE_EVENT")

    async def turn(self, turn_id: str) -> Row:
        return dict(await self.client.turn(turn_id))

    async def export(self, since_utc_ns: int) -> list[Row]:
        return [
            dict(item["event"])
            async for item in self.client.export(self.satellite, since_utc_ns)
        ]

    async def close(self) -> None:
        if self.stream is not None:
            await self.stream.aclose()


def check_events(events: list[Row], turn_id: str) -> None:
    if any(
        event.get("stage") == "unmatched" or event.get("turn_id") != turn_id
        for event in events
    ):
        raise ReplayError("UNMATCHED_TURN")
    wakes = [
        event
        for event in events
        if event.get("stage") == "wake_detected" and event.get("source") == "device"
    ]
    if len(wakes) > 1:
        raise ReplayError("DUPLICATE_WAKE")


async def replay_sample(
    label: Row,
    audio_root: Path,
    wake_audio: Path,
    output: AudioOutput,
    capture: AudioCapture,
    source: DeviceEvents,
) -> Row:
    if check_audio([label], audio_root):
        raise ReplayError("AUDIO_HASH_OR_FORMAT_MISMATCH")
    path = resolve_audio(audio_root, label["audio_path"])
    reference, _ = load_audio(path)
    mark = time.time_ns()
    host = {"mark_start_utc_ns": mark, "mark_start_monotonic_ns": time.monotonic_ns()}
    events: list[Row] = []
    await source.start(mark)
    try:
        await output.play(wake_audio)
        turn_id = None
        while True:
            event = await source.next()
            events.append(event)
            if (
                event.get("stage") == "wake_detected"
                and event.get("source") == "device"
            ):
                if turn_id is not None:
                    raise ReplayError("DUPLICATE_WAKE")
                turn_id = event.get("turn_id")
                if not text(turn_id):
                    raise ReplayError("UNMATCHED_TURN")
            if turn_id is not None:
                check_events(events, turn_id)
            if event.get("stage") == "mic_open" and event.get("source") == "device":
                if turn_id is None:
                    raise ReplayError("MISSING_DEVICE_WAKE_DETECTED")
                break
        await capture.start()
        host["command_play_requested_utc_ns"] = time.time_ns()
        await output.play(path)
        while True:
            event = await source.next()
            events.append(event)
            check_events(events, turn_id)
            if event.get("stage") == "playback_end" and event.get("source") == "device":
                break
        # A device proxy can end before the last acoustic sample.
        await asyncio.sleep(0.25)
        recording = await capture.finish()
        try:
            alignment = align_capture(recording, reference, label["last_voice_sample"])
        finally:
            recording.samples.clear()
        stored = await source.turn(turn_id)
        exported = await source.export(mark)
        check_events(exported, turn_id)
        if stored.get("turn_type") != "voice" or stored.get("turn_id") != turn_id:
            raise ReplayError("UNMATCHED_TURN")
        events = exported
        transcript_events = [
            event for event in events if event.get("stage") == "stt_final"
        ]
        transcript = (
            transcript_events[-1].get("data", {}).get("transcript")
            if transcript_events
            else None
        )
        return {
            "turn_id": turn_id,
            "events": events,
            "recognized_transcript": transcript,
            "alignment": alignment,
            "host_timing": host,
            **{
                stage: next(
                    (
                        event
                        for event in events
                        if event.get("stage") == stage
                        and event.get("source") == "device"
                    ),
                    None,
                )
                for stage in DEVICE_STAGES
            },
        }
    except TimeoutError, StopAsyncIteration:
        raise ReplayError("MISSING_DEVICE_EVENT") from None
    finally:
        await capture.abort()
        await source.close()


def attach_evidence(row: Row, label: Row, records: list[Row]) -> Row:
    for record in records:
        for field in BRIDGE_FIELDS:
            if field not in record:
                raise ReplayError("MISSING_BRIDGE_FIELD: " + field)
    matches = [
        record
        for record in records
        if record.get("turn_id") == row["turn_id"]
        and record.get("run_id") == row["run_id"]
        and record.get("baseline_configuration_id") == row["baseline_config_id"]
    ]
    if len(matches) != 1:
        raise ReplayError("MISSING_OPERATION_EVIDENCE")
    record = matches[0]
    if record["completed"] is not True:
        raise ReplayError("INCOMPLETE_OPERATION_LOG")
    if not isinstance(record["operations"], list):
        raise ReplayError("INVALID_BRIDGE_OPERATIONS")
    trace = []
    for operation in record["operations"]:
        if not isinstance(operation, dict):
            raise ReplayError("INVALID_BRIDGE_OPERATION")
        for field in BRIDGE_OPERATION_MAP:
            if field not in operation:
                raise ReplayError("MISSING_BRIDGE_OPERATION_FIELD: " + field)
        trace.append(
            {
                target: operation[source]
                for source, target in BRIDGE_OPERATION_MAP.items()
            }
        )
    if validate_run_evidence(record) or validate_intent_action_evidence(record, label):
        raise ReplayError("INVALID_OPERATION_EVIDENCE")
    if record["recognized_transcript"] != row["recognized_transcript"]:
        raise ReplayError("TRANSCRIPT_EVIDENCE_MISMATCH")
    return {
        **row,
        "evaluated_intent": record["selected_intent"],
        "action_trace": trace,
        "action_trace_complete": record["completed"],
        "reply_text": record["reply"],
        "reply_char_count": len(record["reply"]),
        "reply_group": record["group"],
    }


def baseline_for(label: Row, config: str, baselines: list[Row]) -> str:
    matches = [
        row
        for row in baselines
        if row.get("phase") == "p0"
        and row.get("clip_id") == label["id"]
        and row.get("audio_sha256") == label["audio_sha256"]
        and row.get("baseline_config_id") == config
        and row.get("split") == label["split"]
        and validate_sample(row)
    ]
    if len(matches) != 1:
        raise ReplayError("MISSING_PAIRED_BASELINE")
    return str(matches[0]["recognized_transcript"])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Device loudspeaker replay; no hardware evidence is bundled."
    )
    result.add_argument("--phase", required=True, choices=("p0", "p1"))
    result.add_argument("--profiles")
    result.add_argument("--labels", type=Path, required=True)
    result.add_argument("--audio-root", type=Path, required=True)
    result.add_argument("--split", choices=("held_out", "tuning"), required=True)
    result.add_argument("--wake-word", action="store_true", required=True)
    result.add_argument("--external-mic", action="store_true", required=True)
    result.add_argument("--limit", type=int)
    result.add_argument("--acceptance", action="store_true")
    result.add_argument("--output", type=Path, required=True)
    return result


def check_options(args: argparse.Namespace) -> list[str]:
    if args.acceptance and args.limit is not None:
        raise ReplayError("LIMIT_NOT_ALLOWED_IN_ACCEPTANCE")
    if args.limit is not None and args.limit <= 0:
        raise ReplayError("INVALID_SMOKE_LIMIT")
    if args.phase == "p0" and args.profiles is not None:
        raise ReplayError("PROFILES_P1_ONLY")
    if args.phase == "p1" and args.profiles != "safe,fast":
        raise ReplayError("P1_REQUIRES_SAFE_FAST")
    return ["baseline"] if args.phase == "p0" else ["safe", "fast"]


async def run(
    args: argparse.Namespace, selector: ProfileSelector | None = None
) -> list[Row]:
    profiles = check_options(args)
    rows = load_corpus(args.labels)
    frozen = frozen_corpus(args.labels, args.labels.parent / "corpus.sha256", rows)
    if not frozen and args.limit is None:
        raise ReplayError("BLOCKED corpus not frozen")
    if not frozen:
        print("NOT FROZEN corpus")
    selected = [
        row
        for row in rows
        if row.get("split") == args.split and row.get("set") == "replay"
    ]
    if not selected:
        raise ReplayError("EMPTY_REPLAY_SPLIT")
    selected = selected[: args.limit]
    if args.phase == "p1" and selector is None:
        await UnavailableProfileSelector().select(profiles[0])
    required = (
        "ASSIST_LIVE_URL",
        "ASSIST_LIVE_TOKEN_FILE",
        "ASSIST_LIVE_SATELLITE",
        "ASSIST_LIVE_WAKE_AUDIO",
        "ASSIST_LIVE_RUN_ID",
        "ASSIST_LIVE_BASELINE_CONFIG_ID",
        "ASSIST_LIVE_CALIBRATION_REVISION",
        "ASSIST_LIVE_OPERATION_RECORDS",
    )
    if any(not os.environ.get(key) for key in required):
        raise ReplayError("REPLAY_SETUP_NOT_AVAILABLE")
    evidence_path = Path(os.environ["ASSIST_LIVE_OPERATION_RECORDS"])
    if evidence_path.exists():
        raise ReplayError("STALE_OPERATION_EVIDENCE")
    config = os.environ["ASSIST_LIVE_BASELINE_CONFIG_ID"]
    baselines = (
        load_corpus(Path(os.environ["ASSIST_LIVE_BASELINE_RUNS"]))
        if args.phase == "p1" and os.environ.get("ASSIST_LIVE_BASELINE_RUNS")
        else []
    )
    token = json.loads(Path(os.environ["ASSIST_LIVE_TOKEN_FILE"]).read_text())["token"]
    pending: list[tuple[Row, Row]] = []
    async with AssistLiveClient(os.environ["ASSIST_LIVE_URL"], token) as client:
        for profile in profiles:
            if selector is not None:
                await selector.select(profile)
            for label in selected:
                baseline = (
                    baseline_for(label, config, baselines)
                    if args.phase == "p1"
                    else None
                )
                sample = await replay_sample(
                    label,
                    args.audio_root,
                    Path(os.environ["ASSIST_LIVE_WAKE_AUDIO"]),
                    AlsaOutput(os.environ.get("ASSIST_LIVE_OUTPUT_DEVICE", "default")),
                    AlsaCapture(
                        os.environ.get("ASSIST_LIVE_CAPTURE_DEVICE", "default")
                    ),
                    ClientEvents(client, os.environ["ASSIST_LIVE_SATELLITE"]),
                )
                if any(row["turn_id"] == sample["turn_id"] for _, row in pending):
                    raise ReplayError("DUPLICATE_REPLAY_TURN")
                pending.append(
                    (
                        label,
                        {
                            **sample,
                            "schema_version": 2,
                            "mode": "smoke" if args.limit is not None else "acceptance",
                            "phase": args.phase,
                            "profile": profile,
                            "clip_id": label["id"],
                            "audio_sha256": label["audio_sha256"],
                            "split": args.split,
                            "frozen": frozen,
                            "run_id": os.environ["ASSIST_LIVE_RUN_ID"],
                            "baseline_config_id": config,
                            "calibration_revision": os.environ[
                                "ASSIST_LIVE_CALIBRATION_REVISION"
                            ],
                            "baseline_transcript": baseline,
                        },
                    )
                )
    if not evidence_path.exists():
        raise ReplayError("MISSING_OPERATION_EVIDENCE")
    records = load_corpus(evidence_path)
    completed = []
    for label, row in pending:
        sample = attach_evidence(row, label, records)
        errors = sample_errors(sample, acceptance=args.limit is None)
        if errors:
            raise ReplayError(",".join(errors))
        completed.append(sample)
    return completed


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        rows = asyncio.run(run(args))
        args.output.write_text(
            "".join(json.dumps(row, allow_nan=False) + "\n" for row in rows)
        )
    except (ReplayError, OSError, ValueError) as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    print(f"PASS replay rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
