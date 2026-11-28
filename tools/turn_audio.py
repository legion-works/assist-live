import asyncio
import cmath
import hashlib
import json
import math
import struct
import wave
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

Row = dict[str, Any]
Docker = Callable[..., Awaitable[str]]
CORRELATION_MINIMUM = 0.18
PEAK_RATIO_MINIMUM = 1.15
WINDOW_TOLERANCE_SAMPLES = 640
RECORDINGS = PurePosixPath("/config/pipeline_recordings")


async def docker_command(*args: str) -> str:
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


async def recording_files(
    docker: Docker, container: str, device_id: str, pipeline_name: str
) -> set[str]:
    root = RECORDINGS / device_id / pipeline_name
    code = (
        "import json,os,sys; "
        "print(json.dumps([os.path.join(r,n) for r,ds,ns in os.walk(sys.argv[1]) "
        "for n in ns if n.startswith('01_stt-') and n.endswith('.wav')]))"
    )
    return set(
        json.loads(await docker("exec", container, "python3", "-c", code, str(root)))
    )


async def copy_recording(
    docker: Docker, container: str, directory: Path, source: str
) -> tuple[Path, str]:
    if (
        not PurePosixPath(source).is_relative_to(RECORDINGS)
        or ".." in PurePosixPath(source).parts
    ):
        raise ValueError("invalid recording path")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    target = directory / (uuid4().hex + "-turn.wav")
    code = (
        "import hashlib,sys; "
        "print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())"
    )
    expected = await docker("exec", container, "python3", "-c", code, source)
    with target.open("xb"):
        target.chmod(0o600)
    try:
        await docker("cp", container + ":" + source, str(target))
    finally:
        target.chmod(0o600)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    after = await docker("exec", container, "python3", "-c", code, source)
    if digest != expected or digest != after:
        raise ValueError("turn audio SHA-256 mismatch; all copies retained")
    return target, digest


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


def correlate(pcm: bytes, source: bytes, *, allow_early_start: bool = False) -> Row:
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
        len(agreed) == 3
        and (allow_early_start or 0 <= offset)
        and offset + len(reference) <= len(recording)
        and all(
            0 <= window["offset_samples"] <= len(recording) - width
            for window in windows
        )
    )
    return result
