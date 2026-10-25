import hashlib
import json
import subprocess
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/annotate_corpus.py"


@pytest.fixture
def annotation_bundle(tmp_path):
    audio = tmp_path / "audio"
    audio.mkdir()
    wav = audio / "test.wav"
    with wave.open(str(wav), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x01\x00" * 16000)
    rows = [
        {
            "id": "clip-01",
            "split": "tuning",
            "audio_path": "test.wav",
            "audio_sha256": hashlib.sha256(wav.read_bytes()).hexdigest(),
            "sample_rate": 16000,
            "last_voice_sample": 12000,
            "last_voice_annotation": "automatic",
            "transcript": "test <script> command",
            "aligned_end_sample": 12800,
            "energy_minus_aligned_samples": -800,
            "boundary_review_required": True,
            "intent": {"name": "HassTurnOff", "slots": {}},
        }
    ]
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps(rows[0]) + "\n")
    return labels, audio, rows


def cli(*args):
    return subprocess.run(
        [sys.executable, str(TOOL), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


def export_file(tmp_path, rows):
    protected = {
        k: v
        for k, v in rows[0].items()
        if k
        not in (
            "last_voice_sample",
            "last_voice_annotation",
            "last_voice_annotator_note",
        )
    }
    digest = hashlib.sha256(
        json.dumps(protected, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    value = {
        "version": 1,
        "split": "tuning",
        "annotator": "annotator-01",
        "rows": [
            {
                "id": "clip-01",
                "audio_sha256": rows[0]["audio_sha256"],
                "row_sha256": digest,
                "frames": 16000,
                "last_voice_sample": 12500,
                "transcript_differs": True,
                "transcript_note": "test command",
                "skip": False,
            }
        ],
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(value))
    return path, value


def test_build_embeds_audio_without_network_and_escapes_transcript(
    annotation_bundle, tmp_path
):
    labels, audio, _ = annotation_bundle
    result = cli(
        "build",
        "--labels",
        labels,
        "--audio-root",
        audio,
        "--split",
        "tuning",
        "--output",
        tmp_path / "page",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    page = (tmp_path / "page/index.html").read_text()
    assert "data:audio/wav;base64," in page
    assert "http://" not in page and "https://" not in page
    assert "<script> command" not in page
    assert "canvas" in page and "localStorage" in page


def test_import_changes_only_manual_fields_and_is_idempotent(
    annotation_bundle, tmp_path
):
    labels, audio, rows = annotation_bundle
    path, _ = export_file(tmp_path, rows)
    result = cli(
        "import",
        "--labels",
        labels,
        "--audio-root",
        audio,
        "--split",
        "tuning",
        "--input",
        path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    actual = json.loads(labels.read_text())
    expected = dict(
        rows[0],
        last_voice_sample=12500,
        last_voice_annotation="manual",
        last_voice_annotator_note={
            "annotator": "annotator-01",
            "transcript_differs": True,
            "transcript_note": "test command",
        },
    )
    assert actual == expected
    before = labels.read_bytes(), labels.stat().st_mtime_ns
    assert (
        cli(
            "import",
            "--labels",
            labels,
            "--audio-root",
            audio,
            "--split",
            "tuning",
            "--input",
            path,
        ).returncode
        == 0
    )
    assert (labels.read_bytes(), labels.stat().st_mtime_ns) == before
    result = cli("compare", "--labels", labels, "--split", "tuning")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "energy n=1 median_ms=31.250 p90_ms=31.250 max_ms=31.250" in result.stdout
    assert "aligned n=1 median_ms=18.750 p90_ms=18.750 max_ms=18.750" in result.stdout
    assert "closest=aligned" in result.stdout


@pytest.mark.parametrize(
    "change",
    [
        "id",
        "sample",
        "bool",
        "annotator",
        "hash",
        "row",
        "frames",
        "extra",
        "duplicate",
        "held_out",
        "note",
        "version",
    ],
)
def test_import_refuses_tampered_export_without_writes(
    annotation_bundle, tmp_path, change
):
    labels, audio, rows = annotation_bundle
    path, value = export_file(tmp_path, rows)
    item = value["rows"][0]
    if change == "id":
        item["id"] = "unknown"
    elif change == "sample":
        item["last_voice_sample"] = 16000
    elif change == "bool":
        item["last_voice_sample"] = True
    elif change == "annotator":
        value["annotator"] = ""
    elif change == "hash":
        item["audio_sha256"] = "0" * 64
    elif change == "row":
        item["row_sha256"] = "0" * 64
    elif change == "frames":
        item["frames"] = 16001
    elif change == "extra":
        item["transcript"] = "replacement"
    elif change == "duplicate":
        value["rows"].append(item)
    elif change == "held_out":
        rows[0]["split"] = "held_out"
        labels.write_text(json.dumps(rows[0]) + "\n")
        protected = {
            key: val
            for key, val in rows[0].items()
            if key
            not in (
                "last_voice_sample",
                "last_voice_annotation",
                "last_voice_annotator_note",
            )
        }
        item["row_sha256"] = hashlib.sha256(
            json.dumps(protected, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    elif change == "note":
        item["transcript_note"] = ""
    else:
        value["version"] = 2
    path.write_text(json.dumps(value))
    before = labels.read_bytes()
    result = cli(
        "import",
        "--labels",
        labels,
        "--audio-root",
        audio,
        "--split",
        "tuning",
        "--input",
        path,
    )
    assert result.returncode == 1
    assert "FAIL" in result.stdout
    assert labels.read_bytes() == before


def test_skip_does_not_change_labels(annotation_bundle, tmp_path):
    labels, audio, rows = annotation_bundle
    path, value = export_file(tmp_path, rows)
    value["rows"][0]["skip"] = True
    value["rows"][0]["transcript_note"] = ""
    path.write_text(json.dumps(value))
    before = labels.read_bytes()
    assert (
        cli(
            "import",
            "--labels",
            labels,
            "--audio-root",
            audio,
            "--split",
            "tuning",
            "--input",
            path,
        ).returncode
        == 0
    )
    assert labels.read_bytes() == before


def test_build_selection_and_git_output_guard(annotation_bundle, tmp_path):
    from tools.annotate_corpus import build, select_rows

    _, audio, rows = annotation_bundle
    other = dict(
        rows[0], id="clip-02", split="held_out", boundary_review_required=False
    )
    assert [r["id"] for r in select_rows(rows + [other], "tuning", None, False)] == [
        "clip-01"
    ]
    assert [r["id"] for r in select_rows(rows + [other], None, "clip-02", False)] == [
        "clip-02"
    ]
    assert [r["id"] for r in select_rows(rows + [other], None, None, True)] == [
        "clip-01"
    ]
    with pytest.raises(ValueError, match="unknown"):
        select_rows(rows, None, "unknown", False)
    output = tmp_path / "repository"
    output.mkdir()
    (output / ".git").write_text("gitdir: synthetic fixture")
    with pytest.raises(ValueError, match="outside Git"):
        build(rows, audio, output / "page", "tuning")
    assert not (output / "page").exists()


def test_compare_percentiles_and_excludes_automatic_rows():
    from tools.annotate_corpus import compare

    rows = [
        {
            "last_voice_annotation": "manual",
            "last_voice_sample": 8000,
            "sample_rate": 16000,
            "aligned_end_sample": 8000 + delta,
            "energy_minus_aligned_samples": 160,
        }
        for delta in (0, 160, 1600)
    ]
    rows.append(
        {
            "last_voice_annotation": "automatic",
            "last_voice_sample": 0,
            "sample_rate": 16000,
            "aligned_end_sample": 160000,
        }
    )
    result = compare(rows)
    assert (
        "aligned n=3 median_ms=10.000 p90_ms=82.000 max_ms=100.000 closest_rows=3"
        in result
    )
    assert (
        "energy n=3 median_ms=20.000 p90_ms=92.000 max_ms=110.000 closest_rows=0"
        in result
    )
    assert "closest=aligned" in result


def test_import_preserves_energy_if_alignment_failed(annotation_bundle, tmp_path):
    labels, audio, rows = annotation_bundle
    rows[0]["aligned_end_sample"] = None
    del rows[0]["energy_minus_aligned_samples"]
    labels.write_text(json.dumps(rows[0]) + "\n")
    path, _ = export_file(tmp_path, rows)
    result = cli(
        "import",
        "--labels",
        labels,
        "--audio-root",
        audio,
        "--split",
        "tuning",
        "--input",
        path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result = cli("compare", "--labels", labels, "--split", "tuning")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "energy n=1 median_ms=31.250" in result.stdout
    before = labels.read_bytes()
    assert (
        cli(
            "import",
            "--labels",
            labels,
            "--audio-root",
            audio,
            "--split",
            "tuning",
            "--input",
            path,
        ).returncode
        == 0
    )
    assert labels.read_bytes() == before


def test_invalid_later_row_does_not_write_earlier_changes(annotation_bundle, tmp_path):
    labels, audio, rows = annotation_bundle
    path, value = export_file(tmp_path, rows)
    value["rows"].append(dict(value["rows"][0], id="unknown"))
    path.write_text(json.dumps(value))
    before = labels.read_bytes()
    assert (
        cli(
            "import",
            "--labels",
            labels,
            "--audio-root",
            audio,
            "--split",
            "tuning",
            "--input",
            path,
        ).returncode
        == 1
    )
    assert labels.read_bytes() == before


def test_compare_does_not_rank_methods_on_different_rows():
    from tools.annotate_corpus import compare

    rows = [
        {
            "last_voice_annotation": "manual",
            "last_voice_sample": 8000,
            "sample_rate": 16000,
            "aligned_end_sample": 8000,
        },
        {
            "last_voice_annotation": "manual",
            "last_voice_sample": 8000,
            "sample_rate": 16000,
            "last_voice_annotator_note": {"automatic_energy_sample": 9000},
        },
    ]
    assert "closest=not-comparable (different row coverage)" in compare(rows)
