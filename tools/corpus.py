import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import wave
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, TypeGuard
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

Row = dict[str, Any]
LICENSES = {"CC0-1.0", "CC-BY-4.0"}
HA_INTENTS = frozenset(
    """
    HassTurnOn HassTurnOff HassGetState HassNevermind HassRespond HassBroadcast
    HassSetPosition HassGetCurrentDate HassGetCurrentTime HassLightSet
    HassClimateSetTemperature HassClimateGetTemperature HassClimateSetFanMode
    HassShoppingListAddItem HassShoppingListCompleteItem HassGetWeather
    HassListAddItem HassListCompleteItem HassListRemoveItem HassVacuumStart
    HassVacuumReturnToBase HassVacuumCleanArea HassMediaPause HassMediaUnpause
    HassMediaNext HassMediaPrevious HassSetVolume HassMediaPlayerMute
    HassMediaPlayerUnmute HassSetVolumeRelative HassMediaSearchAndPlay
    HassStartTimer HassCancelAllTimers HassCancelTimer HassIncreaseTimer
    HassDecreaseTimer HassPauseTimer HassUnpauseTimer HassTimerStatus
    HassFanSetSpeed HassLawnMowerStartMowing HassLawnMowerDock
    """.split()
)
COUNTS = {
    "held_out": {"replay": 200, "pause": 300},
    "tuning": {"replay": 50, "pause": 100},
}


def load_corpus(path: Path) -> list[Row]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            raise ValueError(f"invalid JSON at line {number}") from None
        if not isinstance(item, dict):
            raise ValueError(f"expected object at line {number}")
        rows.append(item)
    return rows


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def integer(value: object) -> TypeGuard[int]:
    return type(value) is int


def safe_locator(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not PurePosixPath(value).is_absolute()
        and not PureWindowsPath(value).drive
        and "\\" not in value
        and all(part not in ("", ".", "..") for part in value.split("/"))
        and value.endswith(".wav")
    )


def public_source(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and not url.fragment
        )
    except ValueError:
        return False


def archive_source(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"zenodo:([1-9][0-9]*)/([a-zA-Z0-9_-]+\.zip)#(.+)", value)
    if match is None or not safe_locator(match[3]):
        return None
    return (
        f"https://zenodo.org/api/records/{match[1]}/files/{match[2]}/content",
        match[3],
    )


def boundary_review_reasons(
    energy_end: int,
    aligned_end: int | None,
    sample_rate: int,
    *,
    alignment_confidence: float,
    minimum_word_confidence: float,
    transcript_matches: bool,
) -> list[str]:
    reasons = []
    if aligned_end is None:
        reasons.append("alignment_failed")
    elif abs(energy_end - aligned_end) * 1000 > 50 * sample_rate:
        reasons.append("boundary_difference")
    if (
        not math.isfinite(alignment_confidence)
        or not 0.5 <= alignment_confidence <= 1
        or not math.isfinite(minimum_word_confidence)
        or not 0.1 <= minimum_word_confidence <= 1
    ):
        reasons.append("low_alignment_confidence")
    if not transcript_matches:
        reasons.append("transcript_mismatch")
    return reasons


def label_errors(row: Row, *, synthetic: bool = False) -> list[str]:
    errors = []
    for field in ("id", "source_revision", "transcript"):
        if not text(row.get(field)):
            errors.append(field)
    if row.get("split") not in ("held_out", "tuning"):
        errors.append("split")
    if row.get("set") not in ("replay", "pause"):
        errors.append("set")
    speaker = row.get("speaker_id")
    pattern = (
        r"synthetic-[a-z0-9]+(?:-[a-z0-9]+)*"
        if synthetic
        else r"speaker-[a-z0-9]+(?:-[a-z0-9]+)*"
    )
    if not isinstance(speaker, str) or not re.fullmatch(pattern, speaker):
        errors.append(
            "synthetic speaker"
            if str(speaker).startswith("synthetic-")
            else "speaker_id"
        )
    if row.get("speaker_type", "synthetic" if synthetic else "human") != (
        "synthetic" if synthetic else "human"
    ):
        errors.append("synthetic speaker_type")
    if not safe_locator(row.get("audio_path")):
        errors.append("audio_path")
    digest = row.get("audio_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        errors.append("audio hash")
    license_id = row.get("license")
    if not isinstance(license_id, str) or license_id not in LICENSES:
        errors.append("license")
    if "attribution" not in row or (
        license_id == "CC-BY-4.0" and not text(row.get("attribution"))
    ):
        errors.append("attribution")
    source = row.get("source_uri")
    if isinstance(source, str) and source.startswith("consent:"):
        consent = row.get("consent_id")
        if (
            not isinstance(consent, str)
            or not re.fullmatch(r"consent-[a-z0-9-]+", consent)
            or source != f"consent:{consent}"
        ):
            errors.append("consent source_uri")
    elif not public_source(source) and archive_source(source) is None:
        errors.append("source_uri")
    elif row.get("consent_id") is not None:
        errors.append("consent_id for public source")
    if "consent_id" not in row:
        errors.append("consent_id")
    if archive_source(source) is not None:
        revision = row.get("source_revision")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
            errors.append("archive source_revision SHA-256")
        if row.get("audio_path") != f"sha256/{digest}.wav":
            errors.append("archive audio_path content hash")
    rate, last = row.get("sample_rate"), row.get("last_voice_sample")
    if not integer(rate) or rate <= 0:
        errors.append("sample_rate")
    if not integer(last) or last < 0:
        errors.append("last_voice_sample")
    if "aligned_end_sample" in row:
        aligned = row["aligned_end_sample"]
        confidence = row.get("alignment_confidence")
        minimum = row.get("alignment_minimum_word_confidence")
        status = row.get("transcript_alignment_status")
        if (
            not (aligned is None or (integer(aligned) and aligned >= 0))
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not isinstance(minimum, (int, float))
            or isinstance(minimum, bool)
            or status not in ("match", "mismatch", "failed")
            or not integer(rate)
            or rate <= 0
            or not integer(last)
        ):
            errors.append("boundary review fields")
        else:
            reasons = boundary_review_reasons(
                last,
                aligned,
                rate,
                alignment_confidence=confidence,
                minimum_word_confidence=minimum,
                transcript_matches=status == "match",
            )
            if (
                type(row.get("boundary_review_required")) is not bool
                or row["boundary_review_required"] != bool(reasons)
                or row.get("boundary_review_reasons") != reasons
            ):
                errors.append("boundary review flag or reasons")
    intent = row.get("intent")
    if (
        not isinstance(intent, dict)
        or not isinstance(intent.get("name"), str)
        or intent["name"] not in HA_INTENTS
        or not isinstance(intent.get("slots"), dict)
    ):
        errors.append("intent")
    if type(row.get("executable_prefix")) is not bool:
        errors.append("executable_prefix")
    if row.get("set") == "pause":
        start, end = row.get("pause_start_sample"), row.get("pause_end_sample")
        if not (
            integer(start)
            and integer(end)
            and integer(rate)
            and integer(last)
            and 0 <= start < end < last
            and 400 * rate <= 1000 * (end - start) <= 800 * rate
        ):
            errors.append("pause sample boundary (400..800 ms)")
        if row.get("pause_annotation") != "manual":
            errors.append("manual pause_annotation")
    elif row.get("executable_prefix") is True:
        errors.append("executable_prefix requires pause set")
    return errors


def validate_rows(rows: list[Row], *, synthetic: bool = False) -> list[str]:
    failures: list[str] = []
    seen: dict[str, set[str]] = {"id": set(), "audio_sha256": set()}
    for number, item in enumerate(rows, 1):
        if not isinstance(item, dict):
            failures.append(f"row {number} object")
            continue
        failures.extend(
            f"row {number} {error}" for error in label_errors(item, synthetic=synthetic)
        )
        for field, values in seen.items():
            value = item.get(field)
            if isinstance(value, str):
                if value in values:
                    failures.append(f"row {number} duplicate {field}")
                values.add(value)
    return failures


def validate_corpus(rows: list[Row]) -> list[str]:
    failures = validate_rows(rows)
    valid = [item for item in rows if isinstance(item, dict) and not label_errors(item)]
    for split, groups in COUNTS.items():
        partition = [item for item in valid if item["split"] == split]
        for group, minimum in groups.items():
            selected = [item for item in partition if item["set"] == group]
            if len(selected) < minimum:
                failures.append(f"{split} {group} count {len(selected)} < {minimum}")
            speakers = len({item["speaker_id"] for item in selected})
            if split == "held_out" and speakers < 10:
                failures.append(f"{split} {group} speakers {speakers} < 10")
        prefixes = sum(
            item["set"] == "pause" and item["executable_prefix"] for item in partition
        )
        if prefixes < 30:
            failures.append(f"{split} prefix count {prefixes} < 30")
    return failures


def resolve_audio(root: Path, locator: str) -> Path:
    if not safe_locator(locator):
        raise ValueError("audio_path")
    path = (root / locator).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("audio_path outside audio root")
    return path


def check_audio(rows: list[Row], root: Path) -> list[str]:
    failures = []
    for number, item in enumerate(rows, 1):
        try:
            path = resolve_audio(root, item.get("audio_path", ""))
            if hashlib.sha256(path.read_bytes()).hexdigest() != item.get(
                "audio_sha256"
            ):
                failures.append(f"row {number} audio hash mismatch")
                continue
            with wave.open(str(path), "rb") as wav:
                if wav.getframerate() != 16000 or wav.getnchannels() != 1:
                    failures.append(f"row {number} WAV must be 16 kHz mono")
                if wav.getframerate() != item.get("sample_rate"):
                    failures.append(f"row {number} WAV sample_rate mismatch")
                last = item.get("last_voice_sample")
                if not integer(last) or not 0 <= last < wav.getnframes():
                    failures.append(f"row {number} sample boundary outside WAV")
                expected = wav.getnframes() * wav.getnchannels() * wav.getsampwidth()
                if len(wav.readframes(wav.getnframes())) != expected:
                    failures.append(f"row {number} truncated WAV")
        except ValueError, RuntimeError:
            failures.append(f"row {number} audio_path")
        except OSError:
            failures.append(f"row {number} unresolved audio")
        except wave.Error, EOFError:
            failures.append(f"row {number} invalid WAV")
    return failures


def check_records(rows: list[Row], licenses: object, consent: object) -> list[str]:
    failures = []
    if (
        not isinstance(licenses, dict)
        or not isinstance(licenses.get("licenses"), list)
        or any(
            not isinstance(item, str) or item not in LICENSES
            for item in licenses["licenses"]
        )
    ):
        failures.append("license records shape or license")
        available: list[str] = []
    else:
        available = licenses["licenses"]
    index = {}
    if not isinstance(consent, list):
        failures.append("consent records shape")
    else:
        for record in consent:
            if (
                not isinstance(record, dict)
                or set(record)
                != {"consent_id", "permitted_use", "license", "attestation"}
                or not all(
                    text(record.get(key))
                    for key in ("consent_id", "permitted_use", "attestation")
                )
                or not isinstance(record.get("license"), str)
                or record["license"] not in LICENSES
            ):
                failures.append("consent record fields")
                continue
            identifier = record["consent_id"]
            if identifier in index:
                failures.append("duplicate consent record")
            index[identifier] = record
    for number, item in enumerate(rows, 1):
        if item.get("license") not in available:
            failures.append(f"row {number} license records missing license")
        source = item.get("source_uri")
        if isinstance(source, str) and source.startswith("consent:"):
            identifier = item.get("consent_id")
            record = index.get(identifier) if isinstance(identifier, str) else None
            if record is None or record["license"] != item.get("license"):
                failures.append(
                    f"row {number} consent record missing or license mismatch"
                )
    return failures


def freeze_manifest(inputs: dict[str, Path], rows: list[Row]) -> str:
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {role}"
        for role, path in sorted(inputs.items())
    ]
    lines.extend(
        f"{item['audio_sha256']}  audio/{item['audio_path']}"
        for item in sorted(rows, key=lambda item: item["audio_path"])
    )
    return "\n".join(lines) + "\n"


def fetch(rows: list[Row], root: Path, *, cache: Path | None = None) -> list[str]:
    failures = validate_rows(rows)
    if failures:
        return failures
    if cache is None:
        cache = Path(
            os.environ.get(
                "CORPUS_CACHE", str(Path.home() / ".cache/assist-live/corpus")
            )
        )
    cache = cache.resolve()
    verified: set[Path] = set()
    for number, item in enumerate(rows, 1):
        try:
            path = resolve_audio(root, item["audio_path"])
            if path.exists():
                continue
            if item["source_uri"].startswith("consent:"):
                failures.append(f"row {number} external consent bundle required")
                continue
            archive = archive_source(item["source_uri"])
            if archive is not None:
                if any(
                    (parent / ".git").exists() for parent in (cache, *cache.parents)
                ):
                    failures.append(f"row {number} archive cache must be outside Git")
                    continue
                url, member = archive
                archive_path = cache / f"{item['source_revision']}.zip"
                if archive_path.is_symlink():
                    failures.append(f"row {number} archive cache symlink")
                    continue
                if archive_path not in verified:
                    cache.mkdir(parents=True, exist_ok=True)
                    if not archive_path.exists():
                        with tempfile.TemporaryFile(dir=cache) as temporary:
                            with urlopen(url, timeout=30) as response:
                                if not public_source(response.geturl()):
                                    failures.append(f"row {number} source_uri redirect")
                                    continue
                                while chunk := response.read(1024 * 1024):
                                    temporary.write(chunk)
                            temporary.seek(0)
                            digest = hashlib.file_digest(
                                temporary, "sha256"
                            ).hexdigest()
                            if digest != item["source_revision"]:
                                failures.append(f"row {number} archive hash mismatch")
                                continue
                            temporary.seek(0)
                            with archive_path.open("wb") as output:
                                while chunk := temporary.read(1024 * 1024):
                                    output.write(chunk)
                    with archive_path.open("rb") as cached:
                        digest = hashlib.file_digest(cached, "sha256").hexdigest()
                    if digest != item["source_revision"]:
                        failures.append(f"row {number} archive hash mismatch")
                        continue
                    verified.add(archive_path)
                with zipfile.ZipFile(archive_path) as bundle:
                    members = [
                        entry for entry in bundle.infolist() if entry.filename == member
                    ]
                    if len(members) != 1 or members[0].file_size > 64 * 1024 * 1024:
                        failures.append(
                            f"row {number} archive member missing or unsafe"
                        )
                        continue
                    data = bundle.read(members[0])
            else:
                with urlopen(item["source_uri"], timeout=30) as response:
                    if not public_source(response.geturl()):
                        failures.append(f"row {number} source_uri redirect")
                        continue
                    data = response.read()
            if hashlib.sha256(data).hexdigest() != item["audio_sha256"]:
                failures.append(f"row {number} fetched audio hash mismatch")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except ValueError, RuntimeError:
            failures.append(f"row {number} audio_path")
        except OSError, URLError:
            failures.append(f"row {number} fetch input")
        except zipfile.BadZipFile, KeyError:
            failures.append(f"row {number} invalid archive member")
    return failures + check_audio(rows, root)


def clopper_pearson(cuts: int, samples: int) -> tuple[float, float]:
    if not 0 <= cuts <= samples or samples <= 0:
        raise ValueError("expected 0 <= cuts <= samples and samples > 0")

    def cdf(k: int, probability: float) -> float:
        return math.fsum(
            math.comb(samples, j) * probability**j * (1 - probability) ** (samples - j)
            for j in range(k + 1)
        )

    def invert(k: int, tail: float) -> float:
        low, high = 0.0, 1.0
        for _ in range(80):
            middle = (low + high) / 2
            if cdf(k, middle) > tail:
                low = middle
            else:
                high = middle
        return (low + high) / 2

    return (
        0.0 if cuts == 0 else invert(cuts - 1, 0.975),
        1.0 if cuts == samples else invert(cuts, 0.025),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check licensed, frozen voice test inputs."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_parser = commands.add_parser("fetch")
    fetch_parser.add_argument("--cache", type=Path)
    check_parser = commands.add_parser("check")
    for command in (fetch_parser, check_parser):
        command.add_argument("--labels", required=True, type=Path)
        command.add_argument("--audio-root", required=True, type=Path)
    for flag in ("synthetic", "licenses", "consent"):
        check_parser.add_argument(f"--{flag}", required=True, type=Path)
    freeze = check_parser.add_mutually_exclusive_group()
    freeze.add_argument("--freeze", type=Path)
    freeze.add_argument("--verify-freeze", type=Path)
    args = parser.parse_args(argv)
    failures = []

    def read_rows(path: Path, role: str) -> list[Row]:
        try:
            return load_corpus(path)
        except OSError, ValueError:
            failures.append(f"{role} input")
            return []

    rows = read_rows(args.labels, "labels")
    if args.command == "fetch":
        failures.extend(fetch(rows, args.audio_root, cache=args.cache))
        success = ["PASS fetched locators (hashes checked)"]
    else:
        synthetic = read_rows(args.synthetic, "synthetic")
        failures.extend(validate_corpus(rows))
        failures.extend(
            f"synthetic {failure}"
            for failure in validate_rows(synthetic, synthetic=True)
        )
        all_rows = rows + synthetic
        if synthetic:
            for field in ("id", "audio_sha256"):
                values = [
                    item.get(field)
                    for item in all_rows
                    if isinstance(item.get(field), str)
                ]
                if len(values) != len(set(values)):
                    failures.append(f"human/synthetic duplicate {field}")
        records = {}
        for role, path in (("license", args.licenses), ("consent", args.consent)):
            try:
                records[role] = json.loads(path.read_text(encoding="utf-8"))
            except OSError, ValueError:
                failures.append(f"{role} records input")
                records[role] = None
        failures.extend(check_records(all_rows, records["license"], records["consent"]))
        audio_failures = check_audio(all_rows, args.audio_root)
        failures.extend(audio_failures)
        if not validate_rows(rows) and not audio_failures:
            for split in COUNTS:
                replay = [
                    r for r in rows if r["split"] == split and r["set"] == "replay"
                ]
                speakers = len({r["speaker_id"] for r in replay})
                if len(replay) >= COUNTS[split]["replay"] and (
                    split != "held_out" or speakers >= 10
                ):
                    print(
                        f"PASS {split} replay count {len(replay)} speakers {speakers}"
                    )
        inputs = {
            "labels": args.labels,
            "synthetic": args.synthetic,
            "licenses": args.licenses,
            "consent": args.consent,
        }
        if args.verify_freeze:
            try:
                if args.verify_freeze.read_text(encoding="utf-8") != freeze_manifest(
                    inputs, all_rows
                ):
                    failures.append("freeze mismatch")
            except OSError, ValueError, KeyError, TypeError:
                failures.append("freeze input")
        if args.freeze and not failures:
            try:
                args.freeze.write_text(
                    freeze_manifest(inputs, all_rows), encoding="utf-8"
                )
            except OSError:
                failures.append("freeze output")
        success = [
            "PASS held_out replay>=200 pause>=300 prefix>=30 speakers>=10",
            "PASS tuning counts",
            "PASS frozen locators",
        ]
    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    for line in success:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
