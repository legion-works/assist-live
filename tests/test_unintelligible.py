import copy
import importlib.util
from pathlib import Path

import pytest

from tests.test_correct_transcripts import records, reference, write_labels
from tests.test_report import build, dataset
from tools import correct_transcripts as transcripts
from tools.corpus import load_corpus

ROOT = Path(__file__).resolve().parents[1]


def unintelligible(label):
    return {
        **reference(label, pending=True),
        "status": "unintelligible",
        "source": "person",
    }


def test_unintelligible_schema_requires_person_and_no_text():
    labels, _ = dataset(1)
    row = unintelligible(labels[0])
    assert not transcripts.validate(records([row]), labels), "UNINTELLIGIBLE_REJECTED"
    assert transcripts.final_text(row) is None
    for change in (
        {"source": None},
        {"source": "engines_agree"},
        {"correct_transcript": "words"},
    ):
        assert transcripts.validate(records([{**row, **change}]), labels), (
            "INVALID_UNINTELLIGIBLE_ACCEPTED"
        )


def test_mark_command_refuses_unknown_and_final_without_writes(tmp_path, capsys):
    assert hasattr(transcripts, "mark_unintelligible"), "MARK_COMMAND_MISSING"
    labels, _ = dataset(2)
    rows = records([reference(labels[0], pending=True), reference(labels[1])])
    data = tmp_path / "correct.jsonl"
    transcripts.write_data(data, rows)
    args = [
        "mark-unintelligible",
        "--labels",
        str(write_labels(tmp_path, labels)),
        "--data",
        str(data),
        "--id",
    ]
    for identifier, error in (
        ("unknown", "unknown"),
        (labels[1]["id"], "already final"),
    ):
        before = data.read_bytes(), data.with_suffix(".sha256").read_bytes()
        assert transcripts.main([*args, identifier]) == 1, "INVALID_MARK_ACCEPTED"
        assert error in capsys.readouterr().out
        assert before == (data.read_bytes(), data.with_suffix(".sha256").read_bytes())
    assert transcripts.main([*args, labels[0]["id"]]) == 0
    marked = load_corpus(data)
    assert marked[1] == unintelligible(labels[0])
    assert transcripts.check_digest(data)
    assert transcripts.main([*args, labels[0]["id"]]) == 1, "MARKED_ROW_REOPENED"
    assert "already unintelligible" in capsys.readouterr().out, (
        "MARK_REFUSAL_NAMES_THE_STATE"
    )


def test_listening_v2_exports_unintelligible_and_checks_stale_rows():
    labels, _ = dataset(1)
    row = reference(labels[0], pending=True)
    rows = records([row])
    document = {
        "version": 2,
        "rows": [
            {
                "id": row["id"],
                "audio_sha256": row["audio_sha256"],
                "row_sha256": transcripts.row_digest(row),
                "status": "unintelligible",
                "correct_transcript": None,
            }
        ],
    }
    assert transcripts.import_listened(rows, document)[1] == unintelligible(
        labels[0]
    ), "UNINTELLIGIBLE_EXPORT_REJECTED"
    assert "Cannot understand" in transcripts.PAGE, "LISTENING_CHOICE_MISSING"
    invalid = copy.deepcopy(document)
    invalid["rows"][0]["correct_transcript"] = "not allowed"
    with pytest.raises(ValueError):
        transcripts.import_listened(rows, invalid)
    document["rows"][0]["row_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="stale"):
        transcripts.import_listened(rows, document)


def test_unintelligible_not_an_accuracy_or_wer_error_and_report_counts():
    labels, rows = dataset(2, "p1")
    baseline_labels, baseline_rows = dataset(2)
    baseline = build(baseline_rows, baseline_labels)
    baseline["accepted"] = True
    refs = records([unintelligible(labels[0]), reference(labels[1])])
    result = build(rows, labels, baseline=baseline, faults={}, correct_transcripts=refs)
    assert result["accuracy"]["rate"] == 1, "UNINTELLIGIBLE_COUNTED_AS_ERROR"
    assert result["accuracy"]["denominator"] == 1, "UNINTELLIGIBLE_IN_DENOMINATOR"
    assert result["accuracy"]["unavailable"] == 0
    assert result["word_error_rate"]["words"] == 4
    assert result["word_error_rate"]["rate"] == 0
    assert result["word_error_rate"]["baseline_rate"] == 0
    assert result.get("unintelligible") == {"held_out": 1, "tuning": 0}, (
        "UNINTELLIGIBLE_REPORT_COUNT_MISSING"
    )
    assert result["sample_count"] == 2, "UNINTELLIGIBLE_REMOVED_FROM_LATENCY"
    assert result["profiles"]["safe"]["sample_count"] == 2
    from tools.report import markdown

    assert "| held_out | 1 |" in markdown(result), (
        "UNINTELLIGIBLE_MARKDOWN_COUNT_MISSING"
    )
    refs[2] = reference(labels[1], pending=True)
    result = build(rows, labels, baseline=baseline, faults={}, correct_transcripts=refs)
    assert result["accuracy"]["rate"] is None, "PENDING_NO_LONGER_FAILS_CLOSED"
    assert result["accuracy"]["unavailable"] == 1
    assert result["word_error_rate"]["rate"] is None
    assert not result["gates"]["accuracy"]


def source_rows():
    return (
        [
            {
                "path": f"test-real/test-{i}.wav",
                "speakerId": f"test-{i % 10}",
                "semantics": {"intent": "SetTimer"},
            }
            for i in range(80)
        ],
        [
            {
                "path": f"train-real/train-{i:04d}.wav",
                "speakerId": f"train-{i % 71:02d}",
                "semantics": {"intent": "SetTimer"},
            }
            for i in range(568)
        ],
    )


def test_selection_extension_is_deterministic_same_part_and_exact_count():
    assert importlib.util.find_spec("tools.timer_selection"), "SELECTION_RULE_MISSING"
    from tools.timer_selection import recording_id, select_timers

    test, train = source_rows()
    base = select_timers(test, train)
    assert len(base) == 250
    held_train = [
        row
        for row in base
        if row["split"] == "held_out" and row["path"].startswith("train-real/")
    ]
    identifiers = {recording_id(row["path"]) for row in held_train[:2]}
    extended = select_timers(test, train, unintelligible_ids=identifiers)
    extras = [
        row for row in extended if row["path"] not in {item["path"] for item in base}
    ]
    assert len(extras) == 2, "REPLACEMENT_COUNT_NOT_EXACT"
    assert all(
        row["split"] == "held_out"
        and row["path"].startswith("train-real/")
        and row["speakerId"] in {item["speakerId"] for item in held_train}
        for row in extras
    ), "REPLACEMENT_FROM_ANOTHER_PART"
    assert {row["selection_position"] for row in extras} == {121, 122}, (
        "REPLACEMENT_SKIPPED_NEXT_ROWS"
    )
    assert (
        select_timers(
            list(reversed(test)), list(reversed(train)), unintelligible_ids=identifiers
        )
        == extended
    ), "REPLACEMENT_NOT_DETERMINISTIC"
    extra_id = recording_id(extras[0]["path"])
    assert (
        len(select_timers(test, train, unintelligible_ids=identifiers | {extra_id}))
        == 253
    )
    tuning = next(row for row in base if row["split"] == "tuning")
    assert (
        select_timers(test, train, unintelligible_ids={recording_id(tuning["path"])})
        == base
    )
    for count in (1, 2):
        fallback = select_timers(
            test,
            train,
            unintelligible_ids={recording_id(row["path"]) for row in test[:count]},
        )
        fallback_extras = [
            row
            for row in fallback
            if row["path"] not in {item["path"] for item in base}
        ]
        assert len(fallback_extras) == count, "EXHAUSTED_TEST_REPLACEMENT_COUNT"
        assert all(
            row["split"] == "held_out" and row["path"].startswith("train-real/")
            for row in fallback_extras
        ), "EXHAUSTED_TEST_USED_TUNING"
        assert {row["selection_position"] for row in fallback_extras} == set(
            range(121, 121 + count)
        ), "EXHAUSTED_TEST_SKIPPED_NEXT_ROW"
    with pytest.raises(ValueError, match="unknown"):
        select_timers(test, train, unintelligible_ids={"unknown"})


def test_corpus_extension_counts_and_transcript_states():
    labels = load_corpus(ROOT / "tests/data/labels.jsonl")
    data = load_corpus(ROOT / "tests/data/correct_transcripts.jsonl")
    assert len(labels) == 251, "CORPUS_REPLACEMENT_MISSING"
    assert len(data) == 252
    assert not transcripts.validate(data, labels)
    by_id = {row["id"]: row for row in data[1:]}
    for split, count, final, unclear in (
        ("held_out", 201, 200, 1),
        ("tuning", 50, 50, 0),
    ):
        refs = [by_id[label["id"]] for label in labels if label["split"] == split]
        assert len(refs) == count
        assert sum(row["status"] == "final" for row in refs) == final, (
            "CORRECT_TRANSCRIPT_COUNT_MISSING"
        )
        assert sum(row["status"] == "unintelligible" for row in refs) == unclear
    assert by_id["tas-v1-a73c7d2f8a71335c7bf9"]["correct_transcript"] is None
