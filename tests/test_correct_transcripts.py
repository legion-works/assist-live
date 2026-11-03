import copy
import importlib.util
import json
from pathlib import Path

import pytest

from tests.test_annotate_corpus import annotation_bundle  # noqa: F401
from tests.test_report import build, dataset

ROOT = Path(__file__).resolve().parents[1]


def api():
    assert importlib.util.find_spec("tools.correct_transcripts"), (
        "TRANSCRIPT_TOOL_MISSING"
    )
    from tools import correct_transcripts

    return correct_transcripts


def identity(family):
    return {
        "family": family,
        "model": family + "-test",
        "code_commit": "a" * 40,
        "model_files": [{"file": "model.bin", "file_sha256": "b" * 64}],
        "settings": "no hints",
    }


def records(rows):
    return [
        {
            "kind": "engines",
            "engine_identities": {
                family: identity(family) for family in ("parakeet", "whisper")
            },
        },
        *rows,
    ]


def reference(label, *, pending=False):
    return {
        "id": label["id"],
        "audio_sha256": label["audio_sha256"],
        "correct_transcript": None if pending else "turn on test light",
        "status": "needs_listening" if pending else "final",
        "source": None if pending else "engines_agree",
        "engines": [
            {"transcript": "Turn on test light.", "engine": "parakeet"},
            {
                "transcript": "turn off test light"
                if pending
                else "turn on test light",
                "engine": "whisper",
            },
        ],
    }


def test_missing_correct_transcripts_make_accuracy_unavailable_not_prompt():
    labels, rows = dataset(2, "p1")
    baseline_labels, baseline_rows = dataset(2)
    baseline = build(baseline_rows, baseline_labels)
    baseline["accepted"] = True
    result = build(rows, labels, baseline=baseline, faults={}, correct_transcripts=None)
    assert result["accuracy"]["rate"] is None, "PROMPT_USED_WITHOUT_CORRECT_TRANSCRIPT"
    assert result["accuracy"]["unavailable"] == 2
    assert result["word_error_rate"]["rate"] is None
    assert not result["gates"]["accuracy"]


def test_report_uses_correct_transcript_for_accuracy_and_both_wer_paths():
    module = api()
    labels, rows = dataset(2, "p1")
    baseline_labels, baseline_rows = dataset(2)
    baseline = build(baseline_rows, baseline_labels)
    baseline["accepted"] = True
    refs = [reference(label) for label in labels]
    for label in labels:
        label["transcript"] = "turn off the prompt light"
    result = build(
        rows, labels, baseline=baseline, faults={}, correct_transcripts=records(refs)
    )
    assert result["accuracy"]["count"] == 2, "ACCURACY_USED_PROMPT"
    assert result["word_error_rate"]["rate"] == 0, "WER_USED_PROMPT"
    assert result["word_error_rate"]["baseline_rate"] == 0, "BASELINE_WER_USED_PROMPT"
    rows[0]["recognized_transcript"] = labels[0]["transcript"]
    result = build(
        rows, labels, baseline=baseline, faults={}, correct_transcripts=records(refs)
    )
    assert result["accuracy"]["count"] == 1, "PROMPT_COUNTED_AS_CORRECT"
    assert module.final_text(refs[0]) == "turn on test light"
    rows[0]["recognized_transcript"] = "turn on test light"
    rows[0]["evaluated_intent"] = {**rows[0]["evaluated_intent"], "name": "HassTurnOff"}
    result = build(
        rows, labels, baseline=baseline, faults={}, correct_transcripts=records(refs)
    )
    assert result["accuracy"]["count"] == 1, "LABEL_INTENT_IGNORED"


def test_pending_correct_transcript_fails_closed_even_if_prompt_matches():
    api()
    labels, rows = dataset(2, "p1")
    baseline_labels, baseline_rows = dataset(2)
    baseline = build(baseline_rows, baseline_labels)
    baseline["accepted"] = True
    refs = [reference(labels[0], pending=True), reference(labels[1])]
    result = build(
        rows, labels, baseline=baseline, faults={}, correct_transcripts=records(refs)
    )
    assert result["accuracy"]["count"] == 1, "PENDING_COUNTED_AS_CORRECT"
    assert result["accuracy"]["rate"] is None, "PENDING_ACCURACY_NOT_UNAVAILABLE"
    assert result["accuracy"]["unavailable"] == 1
    assert result["word_error_rate"]["rate"] is None
    assert not result["gates"]["accuracy"]


def test_reference_schema_ids_status_sources_and_engine_agreement():
    module = api()
    labels, _ = dataset(1)
    row = reference(labels[0])
    assert not module.validate(records([row]), labels), (
        "SHARED_ENGINE_IDENTITIES_REJECTED"
    )
    for change in (
        {"id": "unknown"},
        {"status": "draft"},
        {"source": "prompt"},
        {"correct_transcript": "?!"},
        {"audio_sha256": "c" * 64},
        {"correct_transcript": "turn off test light"},
        {"status": "needs_listening"},
        {"unexpected": True},
    ):
        assert module.validate(records([{**row, **change}]), labels), change
    assert module.validate([], labels)
    assert module.validate(records([row, row]), labels)
    data = records([row])
    data[0]["engine_identities"]["whisper"]["family"] = "parakeet"
    assert module.validate(data, labels)


def test_build_engine_outputs_uses_words_not_prompt_and_checks_audio_ids():
    module = api()
    labels, _ = dataset(1)
    label = labels[0]
    outputs = [
        [
            {
                "id": label["id"],
                "audio_sha256": label["audio_sha256"],
                "transcript": text,
                "engine": identity(family),
            }
        ]
        for text, family in (
            ("Start a timer for 2 minutes.", "parakeet"),
            ("start a timer for two minutes", "whisper"),
        )
    ]
    result = module.from_engines(labels, *outputs)
    assert result[1]["status"] == "final"
    assert result[1]["correct_transcript"] == "Start a timer for 2 minutes."
    outputs[1][0]["transcript"] = "start the timer for two minutes"
    result = module.from_engines(labels, *outputs)
    assert result[1]["status"] == "needs_listening"
    assert result[1]["correct_transcript"] is None
    outputs[1][0]["id"] = "unknown"
    with pytest.raises(ValueError, match="ids"):
        module.from_engines(labels, *outputs)


def test_exactly_one_explicit_engine_header_must_be_first():
    module = api()
    labels, _ = dataset(1)
    data = records([reference(labels[0])])
    assert not module.validate(data, labels), "SHARED_ENGINE_HEADER_REJECTED"
    for invalid in (data[1:], data[1:] + data[:1], [data[0], *data]):
        assert module.validate(invalid, labels), (
            "MISSING_MISPLACED_OR_DUPLICATE_HEADER_ACCEPTED"
        )
    invalid = copy.deepcopy(data)
    del invalid[0]["kind"]
    assert module.validate(invalid, labels), "IMPLICIT_ENGINE_HEADER_ACCEPTED"


def test_clip_cannot_name_engine_absent_from_header():
    module = api()
    labels, _ = dataset(1)
    data = records([reference(labels[0])])
    assert not module.validate(data, labels)
    data[1]["engines"][1]["engine"] = "unknown"
    assert module.validate(data, labels), "UNKNOWN_ENGINE_NAME_ACCEPTED"


def test_import_valid_person_text_unknown_id_and_rejected_text(tmp_path):
    module = api()
    labels, _ = dataset(1)
    row = reference(labels[0], pending=True)
    data = tmp_path / "correct.jsonl"
    module.write_data(data, records([row]))
    exported = {
        "version": 1,
        "rows": [
            {
                "id": row["id"],
                "audio_sha256": row["audio_sha256"],
                "row_sha256": module.row_digest(row),
                "correct_transcript": "turn on a test light",
            }
        ],
    }
    source = tmp_path / "listened.json"

    def run_import(payload):
        source.write_text(json.dumps(payload))
        return module.main(
            [
                "import",
                "--labels",
                str(write_labels(tmp_path, labels)),
                "--data",
                str(data),
                "--input",
                str(source),
            ]
        )

    duplicate = copy.deepcopy(exported)
    duplicate["rows"].append(copy.deepcopy(exported["rows"][0]))
    invalid_payloads = [(duplicate, "DUPLICATE_LISTENING_ID_ACCEPTED")]
    for change, message in (
        ({"id": "unknown"}, "UNKNOWN_LISTENING_ID_ACCEPTED"),
        ({"correct_transcript": "?!"}, "REJECTED_TEXT_ACCEPTED"),
    ):
        invalid = copy.deepcopy(exported)
        invalid["rows"][0].update(change)
        invalid_payloads.append((invalid, message))
    for invalid, message in invalid_payloads:
        before = data.read_bytes()
        assert run_import(invalid) == 1, message
        assert data.read_bytes() == before, "INVALID_IMPORT_CHANGED_DATA"
    assert run_import(exported) == 0
    before = data.read_bytes()
    assert run_import(exported) == 1, "ALREADY_FINAL_ROW_ACCEPTED"
    assert data.read_bytes() == before, "INVALID_IMPORT_CHANGED_DATA"
    final = json.loads(data.read_text().splitlines()[1])
    assert final["status"] == "final"
    assert final["source"] == "person"
    assert final["correct_transcript"] == "turn on a test light"
    assert final["engines"] == row["engines"]


def write_labels(root, labels):
    path = root / "labels.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in labels))
    return path


def test_committed_transcripts_match_labels_and_digest():
    module = api()
    from tools.corpus import load_corpus

    labels = load_corpus(ROOT / "tests/data/labels.jsonl")
    path = ROOT / "tests/data/correct_transcripts.jsonl"
    rows = load_corpus(path)
    assert len(rows) == 251
    assert len(rows[0]["engine_identities"]) == 2
    assert not module.validate(rows, labels)
    assert module.check_digest(path)


def test_changed_data_with_unchanged_pin_is_refused(tmp_path, capsys):
    module = api()
    labels, _ = dataset(1)
    path = tmp_path / "correct_transcripts.jsonl"
    module.write_data(path, records([reference(labels[0])]))
    assert module.check_digest(path)
    path.write_text(path.read_text() + "\n")
    assert not module.check_digest(path), "CHANGED_DATA_ACCEPTED_WITH_STALE_PIN"
    assert (
        module.main(
            [
                "check",
                "--labels",
                str(write_labels(tmp_path, labels)),
                "--data",
                str(path),
            ]
        )
        == 1
    )
    assert "digest mismatch" in capsys.readouterr().out


@pytest.mark.parametrize(
    "change",
    [
        {"row_sha256": "c" * 64},
        {"audio_sha256": "c" * 64},
        {"correct_transcript": ""},
        {"correct_transcript": None},
        {"correct_transcript": 42},
        {"unknown_field": True},
    ],
)
def test_import_refuses_stale_or_invalid_rows_before_any_change(change):
    module = api()
    labels, _ = dataset(2)
    rows = records([reference(label, pending=True) for label in labels])
    original = copy.deepcopy(rows)
    document = {
        "version": 1,
        "rows": [
            {
                "id": row["id"],
                "audio_sha256": row["audio_sha256"],
                "row_sha256": module.row_digest(row),
                "correct_transcript": "turn on test light",
            }
            for row in rows[1:]
        ],
    }
    document["rows"][1].update(change)
    with pytest.raises(ValueError):
        module.import_listened(rows, document)
    assert rows == original


def test_listening_page_contains_pending_audio_prompt_and_both_texts(request, tmp_path):
    module = api()
    _, audio, labels = request.getfixturevalue("annotation_bundle")
    rows = records([reference(labels[0], pending=True)])
    output = tmp_path / "listen.html"
    module.listening_page(labels, rows, audio, output)
    page = output.read_text()
    assert "data:audio/wav;base64," in page
    assert "\\u003cscript>" in page
    assert "turn off test light" in page
    assert "Turn on test light." in page
    assert "row_sha256" in page
    assert "createElement('audio')" in page
    assert "use.dataset.pick = name" in page, "PICK_BUTTON_FOR_EACH_TEXT"
    assert "localStorage.setItem(key(row), input.value)" in page, (
        "TYPED_TEXT_KEPT_ACROSS_RELOAD"
    )
    assert "Nothing to export" in page, "EMPTY_EXPORT_REFUSED"


@pytest.mark.parametrize("command", ["listen", "import"])
def test_review_commands_refuse_changed_data_with_stale_pin(command, request, tmp_path):
    module = api()
    label_path, audio, labels = request.getfixturevalue("annotation_bundle")
    path = tmp_path / "correct_transcripts.jsonl"
    module.write_data(path, records([reference(labels[0], pending=True)]))
    path.write_text(path.read_text() + "\n")
    exported = tmp_path / "export.json"
    exported.write_text(json.dumps({"version": 1, "rows": []}))
    args = [command, "--labels", str(label_path), "--data", str(path)]
    args += (
        ["--input", str(exported)]
        if command == "import"
        else ["--audio-root", str(audio), "--output", str(tmp_path / "review.html")]
    )
    before = path.read_bytes()
    assert module.main(args) == 1, "REVIEW_COMMAND_ACCEPTED_CHANGED_DATA_WITH_STALE_PIN"
    assert path.read_bytes() == before
