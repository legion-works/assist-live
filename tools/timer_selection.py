import argparse
import ast
import csv
import hashlib
import io
import json
import random
import sys
import zipfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.corpus import Row, load_corpus
from tools.correct_transcripts import check_digest, split_records, validate

ARCHIVE_SHA256 = "c92772c830109a6d685b2106fa3f28fc7b1184bc74722045282ed7efb0cf135a"


def recording_id(path: str) -> str:
    return "tas-v1-" + hashlib.sha256(path.encode()).hexdigest()[:20]


def select_timers(
    test_rows: list[Row],
    train_rows: list[Row],
    *,
    unintelligible_ids: set[str] | None = None,
) -> list[Row]:
    source = []
    for rows in (test_rows, train_rows):
        timers = []
        for row in rows:
            semantics = row["semantics"]
            if isinstance(semantics, str):
                semantics = ast.literal_eval(semantics)
            if semantics["intent"] == "SetTimer":
                timers.append(row)
        source.append(sorted(timers, key=lambda row: row["path"]))
    test, train = source
    test_speakers = {row["speakerId"] for row in test}
    train_speakers = sorted({row["speakerId"] for row in train} - test_speakers)
    random.Random(4662102).shuffle(train_speakers)
    held_speakers = set(train_speakers[:20])
    held_pool = [row for row in train if row["speakerId"] in held_speakers]
    random.Random(4662103).shuffle(held_pool)
    tuning_pool = [
        row for row in train if row["speakerId"] not in test_speakers | held_speakers
    ]
    random.Random(4662104).shuffle(tuning_pool)
    unclear = unintelligible_ids or set()
    held_ids = {recording_id(row["path"]) for row in test + held_pool}
    extra = len(unclear & held_ids)
    if len(test) != 80 or len(held_pool) < 120 + extra or len(tuning_pool) < 50:
        raise ValueError("insufficient timer selection pool")
    selected = [
        {**row, "split": split, "selection_position": position}
        for split, pool in (
            ("held_out", test),
            ("held_out", held_pool[: 120 + extra]),
            ("tuning", tuning_pool[:50]),
        )
        for position, row in enumerate(pool, 1)
    ]
    if not unclear <= {recording_id(row["path"]) for row in selected}:
        raise ValueError("unknown or unselected unintelligible id")
    return sorted(selected, key=lambda row: (row["split"], row["path"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--correct-transcripts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        labels = load_corpus(args.labels)
        data = load_corpus(args.correct_transcripts)
        failures = validate(data, labels)
        if failures or not check_digest(args.correct_transcripts):
            raise ValueError("invalid correct transcripts or digest")
        with args.archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != ARCHIVE_SHA256:
                raise ValueError("archive hash mismatch")
        with zipfile.ZipFile(args.archive) as bundle:
            source = [
                list(csv.DictReader(io.StringIO(bundle.read(part + ".csv").decode())))
                for part in ("test-real", "train-real")
            ]
        unclear = {
            row["id"]
            for row in split_records(data)[1]
            if row["status"] == "unintelligible"
        }
        selected = select_timers(*source, unintelligible_ids=unclear)
        output = args.output.resolve()
        if any((parent / ".git").exists() for parent in (output, *output.parents)):
            raise ValueError("selection output must be outside Git")
        with output.open("x") as stream:
            stream.write(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in selected)
            )
        print(f"PASS timer selection: {len(selected)} recordings")
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        SyntaxError,
        zipfile.BadZipFile,
    ) as error:
        print(f"FAIL timer selection: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
