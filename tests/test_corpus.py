import hashlib
import io
import json
import struct
import subprocess
import sys
import wave
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/corpus.py"


def test_small_corpus_is_rejected():
    from tools.corpus import validate_corpus

    assert validate_corpus([])


def row(number=0, split="held_out", group="pause"):
    return {
        "id": f"clip-{number:04d}",
        "speaker_id": f"speaker-{number % 10:02d}",
        "set": group,
        "audio_path": f"sha256/clip-{number:04d}.wav",
        "source_uri": "consent:consent-01",
        "split": split,
        "audio_sha256": f"{number:064x}",
        "sample_rate": 16000,
        "last_voice_sample": 14000,
        "last_voice_annotation": "manual",
        "transcript": "turn off the test light in five minutes",
        "intent": {
            "name": "HassTurnOff",
            "slots": {"name": "test light", "delay_seconds": 300},
        },
        "pause_start_sample": 3200,
        "pause_end_sample": 12800,
        "pause_annotation": "manual",
        "executable_prefix": group == "pause",
        "license": "CC-BY-4.0",
        "consent_id": "consent-01",
        "source_revision": "bundle-01",
        "attribution": "Anonymous volunteer, consent-01",
    }


def full_rows():
    rows = []
    for split, replay, pause in (("held_out", 200, 300), ("tuning", 50, 100)):
        for group, count in (("replay", replay), ("pause", pause)):
            for _ in range(count):
                rows.append(row(len(rows), split, group))
    return rows


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("consent_id", "", "consent"),
        ("source_uri", "consent:other", "consent"),
        ("license", "CC-BY-NC-4.0", "license"),
        ("license", "CC-BY-SA-4.0", "license"),
        ("attribution", "", "attribution"),
        ("last_voice_sample", -1, "last_voice_sample"),
        ("sample_rate", True, "sample_rate"),
        ("pause_start_sample", -1, "pause"),
        ("pause_end_sample", 3200, "pause"),
        ("pause_end_sample", 15999, "pause"),
        ("pause_annotation", "asr", "manual"),
        ("intent", {}, "intent"),
        ("intent", {"name": "HassTurnOff", "slots": []}, "intent"),
        ("audio_sha256", "not-a-hash", "hash"),
        ("speaker_id", "", "speaker"),
        ("speaker_id", "A Person", "speaker"),
        ("speaker_id", "synthetic-01", "synthetic"),
        ("speaker_type", "synthetic", "synthetic"),
        ("audio_path", "/outside.wav", "audio_path"),
        ("audio_path", "../outside.wav", "audio_path"),
        ("audio_path", "sha256/../outside.wav", "audio_path"),
        ("audio_path", "C:\\outside.wav", "audio_path"),
        ("source_uri", "", "source_uri"),
        ("source_revision", "", "source_revision"),
        ("split", "test", "split"),
        ("id", "", "id"),
        ("transcript", "", "transcript"),
        ("executable_prefix", "true", "executable_prefix"),
    ],
)
def test_invalid_labels_are_rejected(field, value, reason):
    from tools.corpus import validate_corpus

    rows = full_rows()
    rows[200][field] = value
    assert any(reason in failure for failure in validate_corpus(rows))


@pytest.mark.parametrize("field", list(row()))
def test_missing_fields_are_rejected(field):
    from tools.corpus import validate_corpus

    rows = full_rows()
    del rows[200][field]
    assert validate_corpus(rows)


def test_partition_counts_and_speaker_counts_are_independent():
    from tools.corpus import validate_corpus

    rows = full_rows()
    assert validate_corpus(rows) == []
    for item in rows:
        if item["split"] == "held_out":
            item["speaker_id"] = "speaker-01"
    assert any("held_out replay speakers" in f for f in validate_corpus(rows))
    assert any("held_out pause speakers" in f for f in validate_corpus(rows))
    assert validate_corpus([r for r in full_rows() if r["split"] == "tuning"])
    rows = full_rows()
    for item in rows:
        item["executable_prefix"] = False
    failures = validate_corpus(rows)
    assert any("held_out prefix" in f for f in failures)
    assert any("tuning prefix" in f for f in failures)


@pytest.mark.parametrize("field", ["id", "audio_sha256"])
def test_duplicate_recordings_are_rejected(field):
    from tools.corpus import validate_corpus

    rows = full_rows()
    rows[-1][field] = rows[0][field]
    assert any("duplicate" in f and field in f for f in validate_corpus(rows))


def test_pause_limits_are_inclusive_and_sample_based():
    from tools.corpus import validate_corpus

    for samples, valid in ((6399, False), (6400, True), (12800, True), (12801, False)):
        rows = full_rows()
        rows[200]["pause_start_sample"] = 0
        rows[200]["pause_end_sample"] = samples
        assert (validate_corpus(rows) == []) is valid


def test_public_sources_require_a_pinned_https_url():
    from tools.corpus import validate_corpus

    rows = full_rows()
    rows[0]["source_uri"] = "https://data.example/clip.wav?revision=1"
    rows[0]["consent_id"] = None
    assert validate_corpus(rows) == []
    rows[0]["source_uri"] = "file:///outside.wav"
    assert any("source_uri" in f for f in validate_corpus(rows))


@pytest.fixture
def bundle(tmp_path):
    audio = tmp_path / "audio"
    (audio / "sha256").mkdir(parents=True)
    rows = full_rows()
    for number, item in enumerate(rows):
        path = audio / item["audio_path"]
        with wave.open(str(path), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(struct.pack("<h", number) + b"\x00\x00" * 15999)
        item["audio_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    labels = tmp_path / "labels.jsonl"
    write_labels(labels, rows)
    synthetic = tmp_path / "synthetic.jsonl"
    synthetic.write_text("")
    licenses = tmp_path / "LICENSES.json"
    licenses.write_text(json.dumps({"licenses": ["CC0-1.0", "CC-BY-4.0"]}))
    consent = tmp_path / "consent-index.json"
    consent.write_text(
        json.dumps(
            [
                {
                    "consent_id": "consent-01",
                    "permitted_use": "recording, redistribution, and test playback",
                    "license": "CC-BY-4.0",
                    "attestation": "Written consent checked. Identity redacted.",
                }
            ]
        )
    )
    return tmp_path, rows


def write_labels(path, rows):
    path.write_text("".join(json.dumps(item) + "\n" for item in rows))


def run_cli(root, *extra, command="check"):
    args = [
        sys.executable,
        str(TOOL),
        command,
        "--labels",
        str(root / "labels.jsonl"),
        "--audio-root",
        str(root / "audio"),
    ]
    if command == "check":
        args += [
            "--synthetic",
            str(root / "synthetic.jsonl"),
            "--licenses",
            str(root / "LICENSES.json"),
            "--consent",
            str(root / "consent-index.json"),
        ]
    return subprocess.run(
        args + list(extra), capture_output=True, text=True, check=False
    )


def test_complete_corpus_freezes_and_verifies_without_rewrite(bundle):
    root, _ = bundle
    freeze = root / "corpus.sha256"
    result = run_cli(root, "--freeze", str(freeze))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "PASS held_out replay>=200 pause>=300 prefix>=30 speakers>=10" in result.stdout
    )
    assert "PASS tuning counts" in result.stdout
    assert "PASS frozen locators" in result.stdout
    before = freeze.stat().st_mtime_ns, freeze.read_bytes()
    result = run_cli(root, "--verify-freeze", str(freeze))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (freeze.stat().st_mtime_ns, freeze.read_bytes()) == before
    result = run_cli(root, command="fetch")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "change", ["partition", "consent", "licenses", "synthetic", "digest"]
)
def test_freeze_covers_all_inputs_and_never_rewrites(bundle, change):
    root, rows = bundle
    freeze = root / "corpus.sha256"
    assert run_cli(root, "--freeze", str(freeze)).returncode == 0
    if change == "partition":
        rows[0]["split"], rows[500]["split"] = "tuning", "held_out"
        write_labels(root / "labels.jsonl", rows)
    elif change == "digest":
        freeze.write_text("0" * 64 + "  labels\n")
    else:
        file = {
            "consent": "consent-index.json",
            "licenses": "LICENSES.json",
            "synthetic": "synthetic.jsonl",
        }[change]
        with (root / file).open("a") as stream:
            stream.write("\n")
    before = freeze.stat().st_mtime_ns, freeze.read_bytes()
    result = run_cli(root, "--verify-freeze", str(freeze))
    assert result.returncode == 1
    assert "FAIL freeze mismatch" in result.stdout
    assert (freeze.stat().st_mtime_ns, freeze.read_bytes()) == before


@pytest.mark.parametrize(
    "change,reason",
    [
        ("missing", "unresolved audio"),
        ("hash", "audio hash"),
        ("boundary", "sample boundary"),
        ("rate", "sample_rate"),
        ("symlink", "audio_path"),
        ("consent", "consent record"),
        ("license", "license records"),
    ],
)
def test_file_and_record_failures_prevent_freeze(bundle, change, reason):
    root, rows = bundle
    path = root / "audio" / rows[0]["audio_path"]
    if change == "missing":
        path.unlink()
    elif change == "hash":
        path.write_bytes(b"changed")
    elif change == "boundary":
        rows[0]["last_voice_sample"] = 16000
    elif change == "rate":
        rows[0]["sample_rate"] = 8000
    elif change == "symlink":
        outside = root / "outside.wav"
        path.rename(outside)
        path.symlink_to(outside)
    elif change == "consent":
        (root / "consent-index.json").write_text("[]")
    else:
        (root / "LICENSES.json").write_text('{"licenses": ["CC0-1.0"]}')
    write_labels(root / "labels.jsonl", rows)
    freeze = root / "corpus.sha256"
    result = run_cli(root, "--freeze", str(freeze))
    assert result.returncode == 1
    assert reason in result.stdout
    assert not freeze.exists()
    assert "Traceback" not in result.stderr


def test_synthetic_rows_cannot_supply_human_counts(bundle):
    root, rows = bundle
    for item in rows:
        item["speaker_id"] = "synthetic-01"
        item["speaker_type"] = "synthetic"
    write_labels(root / "synthetic.jsonl", rows)
    (root / "labels.jsonl").write_text("")
    result = run_cli(root)
    assert result.returncode == 1
    assert "FAIL held_out replay count 0 < 200" in result.stdout
    assert "FAIL tuning pause count 0 < 100" in result.stdout


def test_invalid_json_and_missing_files_are_named_failures(tmp_path):
    from tools.corpus import load_corpus

    labels = tmp_path / "labels.jsonl"
    labels.write_text("[]\n")
    with pytest.raises(ValueError, match="line 1"):
        load_corpus(labels)
    labels.write_text("{broken\n")
    with pytest.raises(ValueError, match="line 1"):
        load_corpus(labels)
    result = run_cli(tmp_path)
    assert result.returncode == 1
    assert "FAIL labels input" in result.stdout
    assert "FAIL synthetic input" in result.stdout
    assert "FAIL license records" in result.stdout
    assert "FAIL consent records" in result.stdout
    assert "Traceback" not in result.stderr


def test_fetch_requires_external_volunteer_bundle(bundle):
    root, rows = bundle
    (root / "audio" / rows[0]["audio_path"]).unlink()
    result = run_cli(root, command="fetch")
    assert result.returncode == 1
    assert "external consent bundle required" in result.stdout


@pytest.mark.parametrize("valid_hash", [True, False])
def test_fetch_public_bytes_checks_hash_before_write(tmp_path, monkeypatch, valid_hash):
    from tools import corpus

    data = b"wave transport fixture"
    item = row()
    item["source_uri"] = "https://data.example/clip.wav?revision=1"
    item["consent_id"] = None
    item["audio_sha256"] = hashlib.sha256(data if valid_hash else b"other").hexdigest()

    class Response(io.BytesIO):
        def geturl(self):
            return item["source_uri"]

    monkeypatch.setattr(corpus, "urlopen", lambda url, timeout: Response(data))
    failures = corpus.fetch([item], tmp_path)
    path = tmp_path / item["audio_path"]
    if valid_hash:
        assert path.read_bytes() == data
        assert any("invalid WAV" in failure for failure in failures)
    else:
        assert not path.exists()
        assert any("fetched audio hash mismatch" in failure for failure in failures)


def test_consent_index_rejects_extra_private_fields(bundle):
    root, _ = bundle
    consent = root / "consent-index.json"
    records = json.loads(consent.read_text())
    records[0]["legal_identity"] = "forbidden field"
    consent.write_text(json.dumps(records))
    result = run_cli(root)
    assert result.returncode == 1
    assert "consent record fields" in result.stdout


@pytest.mark.parametrize("field", list(row()))
def test_malformed_field_types_never_raise(field):
    from tools.corpus import validate_corpus

    item = row()
    item[field] = []
    assert validate_corpus([item])


def test_truncated_wave_cannot_supply_sample_boundaries(bundle):
    root, rows = bundle
    path = root / "audio" / rows[0]["audio_path"]
    path.write_bytes(path.read_bytes()[:100])
    rows[0]["audio_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_labels(root / "labels.jsonl", rows)
    result = run_cli(root)
    assert result.returncode == 1
    assert "truncated WAV" in result.stdout


def test_clopper_pearson_pause_arithmetic():
    from tools.corpus import clopper_pearson

    assert clopper_pearson(3, 100)[1] * 100 == pytest.approx(8.5176, abs=0.0001)
    assert clopper_pearson(9, 300)[1] * 100 == pytest.approx(5.61835, abs=0.00001)
    assert 9 / 300 == 0.03
    accepted = [
        k for k in range(101) if k / 100 <= 0.03 and clopper_pearson(k, 100)[1] <= 0.06
    ]
    assert accepted == [0, 1]
    assert clopper_pearson(0, 100)[0] == 0
    assert clopper_pearson(100, 100)[1] == 1
    assert clopper_pearson(50, 100)[0] == pytest.approx(0.398321, abs=0.000001)
    with pytest.raises(ValueError):
        clopper_pearson(1, 0)


@pytest.fixture
def archive_bundle(tmp_path, monkeypatch):
    from tools import corpus

    wav = io.BytesIO()
    with wave.open(wav, "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x00\x00" * 16000)
    data = wav.getvalue()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("test-real/clip.wav", data)
        bundle.writestr("LICENSE", b"Test CC0 license\n")
    payload = archive.getvalue()
    item = row(group="replay")
    item.update(
        source_uri="zenodo:4662102/timers-and-such-real.zip#test-real/clip.wav",
        source_revision=hashlib.sha256(payload).hexdigest(),
        consent_id=None,
        audio_sha256=hashlib.sha256(data).hexdigest(),
        audio_path=f"sha256/{hashlib.sha256(data).hexdigest()}.wav",
        last_voice_method="rms-10ms-max-0.005-0.08peak",
        last_voice_parameters={
            "absolute_threshold": 0.005,
            "relative_threshold": 0.08,
            "window_ms": 10,
        },
        last_voice_parameter_sha256="0" * 64,
        last_voice_crosscheck_method="torchaudio-2.7.1-MMS_FA-CTC-with-known-transcript",
        alignment_model_sha256="1" * 64,
        alignment_frame_samples=320.0,
        alignment_transcript=["synthetic", "test"],
    )

    class Response(io.BytesIO):
        def geturl(self):
            return "https://zenodo.org/api/records/4662102/files/timers-and-such-real.zip/content"

    monkeypatch.setattr(corpus, "urlopen", lambda url, timeout: Response(payload))
    records = tmp_path / "LICENSES.json"
    records.write_text(
        json.dumps(
            {
                "licenses": ["CC0-1.0", "CC-BY-4.0"],
                "sources": [
                    {
                        "source": "Synthetic test archive",
                        "license": "CC-BY-4.0",
                        "archive_url": Response().geturl(),
                        "archive_sha256": item["source_revision"],
                        "archive_md5": hashlib.md5(payload).hexdigest(),
                        "archive_size": len(payload),
                        "license_member": "LICENSE",
                        "license_text": "Test CC0 license\n",
                        "license_sha256": hashlib.sha256(
                            b"Test CC0 license\n"
                        ).hexdigest(),
                        "credit": "Synthetic test credit",
                        "consent_source": "https://data.example/consent",
                        "consent_quote": "Synthetic test permission",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(corpus, "DEFAULT_LICENSES", records, raising=False)
    return item, payload, data


def test_fetch_archive_member_and_reuse_verified_cache(
    tmp_path, monkeypatch, archive_bundle
):
    from tools import corpus

    item, _, data = archive_bundle
    cache = tmp_path / "cache"
    assert corpus.fetch([item], tmp_path / "audio", cache=cache) == []
    assert (tmp_path / "audio" / item["audio_path"]).read_bytes() == data
    monkeypatch.setattr(
        corpus, "urlopen", lambda *a, **k: pytest.fail("network reused")
    )
    assert corpus.fetch([item], tmp_path / "second", cache=cache) == []
    assert (tmp_path / "second" / item["audio_path"]).read_bytes() == data


@pytest.mark.parametrize("change", ["archive", "member", "cache"])
def test_archive_hashes_checked_before_member_write(tmp_path, archive_bundle, change):
    from tools import corpus

    item, _, _ = archive_bundle
    cache = tmp_path / "cache"
    if change == "archive":
        item["source_revision"] = "0" * 64
    elif change == "member":
        item["audio_sha256"] = "0" * 64
        item["audio_path"] = f"sha256/{'0' * 64}.wav"
    else:
        cache.mkdir()
        (cache / f"{item['source_revision']}.zip").write_bytes(b"corrupted cache")
    failures = corpus.fetch([item], tmp_path / "audio", cache=cache)
    assert any("hash mismatch" in failure for failure in failures)
    assert not (tmp_path / "audio" / item["audio_path"]).exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_revision", "not-a-hash"),
        ("source_uri", "zenodo:4662102/timers-and-such-real.zip#../clip.wav"),
        ("source_uri", "zenodo:4662102/timers-and-such-real.zip#/clip.wav"),
        ("source_uri", "zenodo:4662102/timers-and-such-real.zip#test-real/missing.wav"),
        ("audio_path", "sha256/not-the-content-hash.wav"),
    ],
)
def test_archive_locator_rejections(tmp_path, archive_bundle, field, value):
    from tools import corpus

    item, _, _ = archive_bundle
    item[field] = value
    if "missing.wav" not in value:
        assert corpus.validate_rows([item])
    assert corpus.fetch([item], tmp_path / "audio", cache=tmp_path / "cache")
    assert not (tmp_path / "audio").exists()


def test_unknown_intent_name_is_rejected():
    from tools.corpus import label_errors

    item = row()
    item["intent"] = {"name": "SetTimer", "slots": {"minutes": 5}}
    assert "intent" in label_errors(item)
    item["intent"]["name"] = "HassStartTimer"
    assert "intent" not in label_errors(item)


@pytest.mark.parametrize("channels,rate", [(2, 16000), (1, 8000)])
def test_audio_rejects_non_mono_or_non_16khz(tmp_path, channels, rate):
    from tools.corpus import check_audio

    item = row(group="replay")
    item["sample_rate"] = rate
    path = tmp_path / item["audio_path"]
    path.parent.mkdir()
    with wave.open(str(path), "wb") as wav:
        wav.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x00" * 16000 * channels)
    item["audio_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert any("16 kHz mono" in failure for failure in check_audio([item], tmp_path))


def test_replay_only_reports_pass_counts_but_refuses_freeze(bundle):
    root, rows = bundle
    write_labels(root / "labels.jsonl", [r for r in rows if r["set"] == "replay"])
    freeze = root / "no-freeze.sha256"
    result = run_cli(root, "--freeze", str(freeze))
    assert result.returncode == 1
    assert "PASS held_out replay count 200 speakers 10" in result.stdout
    assert "PASS tuning replay count 50 speakers 10" in result.stdout
    assert "FAIL held_out pause count 0 < 300" in result.stdout
    assert "FAIL tuning prefix count 0 < 30" in result.stdout
    assert not freeze.exists()


def test_archive_cache_must_stay_outside_git(tmp_path, archive_bundle):
    from tools import corpus

    item, _, _ = archive_bundle
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").write_text("gitdir: fixture")
    failures = corpus.fetch([item], tmp_path / "audio", cache=repository / "cache")
    assert any("outside Git" in failure for failure in failures)
    assert not (repository / "cache").exists()


def test_archive_cache_symlink_cannot_hide_git_parent(tmp_path, archive_bundle):
    from tools import corpus

    item, _, _ = archive_bundle
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    hidden = repository / "nested"
    hidden.mkdir()
    cache = tmp_path / "cache"
    cache.symlink_to(hidden, target_is_directory=True)
    failures = corpus.fetch([item], tmp_path / "audio", cache=cache)
    assert any("outside Git" in failure for failure in failures)
    assert list(hidden.iterdir()) == []


@pytest.mark.parametrize("kind", ["invalid", "duplicate", "oversized"])
def test_archive_rejects_invalid_or_unsafe_members(tmp_path, archive_bundle, kind):
    from tools import corpus

    item, _, data = archive_bundle
    payload = io.BytesIO()
    if kind == "invalid":
        payload.write(b"not a ZIP")
    else:
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("test-real/clip.wav", data)
            bundle.writestr("LICENSE", b"Test CC0 license\n")
            if kind == "duplicate":
                with pytest.warns(UserWarning, match="Duplicate name"):
                    bundle.writestr("test-real/clip.wav", data)
            else:
                bundle.writestr("test-real/large.wav", b"\x00" * (64 * 1024 * 1024 + 1))
                item["source_uri"] = (
                    "zenodo:4662102/timers-and-such-real.zip#test-real/large.wav"
                )
    item["source_revision"] = hashlib.sha256(payload.getvalue()).hexdigest()
    records = json.loads((tmp_path / "LICENSES.json").read_text())
    records["sources"][0]["archive_sha256"] = item["source_revision"]
    records["sources"][0]["archive_size"] = len(payload.getvalue())
    (tmp_path / "LICENSES.json").write_text(json.dumps(records))
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"{item['source_revision']}.zip").write_bytes(payload.getvalue())
    failures = corpus.fetch([item], tmp_path / "audio", cache=cache)
    expected = "invalid archive member" if kind == "invalid" else "missing or unsafe"
    assert any(expected in failure for failure in failures)
    assert not (tmp_path / "audio").exists()


def test_archive_rejects_non_https_redirect(tmp_path, monkeypatch, archive_bundle):
    from tools import corpus

    item, payload, _ = archive_bundle

    class Response(io.BytesIO):
        def geturl(self):
            return "http://data.example/archive.zip"

    monkeypatch.setattr(corpus, "urlopen", lambda url, timeout: Response(payload))
    failures = corpus.fetch([item], tmp_path / "audio", cache=tmp_path / "cache")
    assert any("source_uri redirect" in failure for failure in failures)
    assert not (tmp_path / "audio").exists()


def test_archive_file_symlink_cannot_write_into_git(tmp_path, archive_bundle):
    from tools import corpus

    item, _, _ = archive_bundle
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    target = repository / "archive.zip"
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"{item['source_revision']}.zip").symlink_to(target)
    failures = corpus.fetch([item], tmp_path / "audio", cache=cache)
    assert any("archive cache symlink" in failure for failure in failures)
    assert not target.exists()


@pytest.mark.parametrize(
    "offset,score,minimum,matched,aligned,reasons",
    [
        (800, 0.5, 0.1, True, 10000, []),
        (-800, 0.9, 0.9, True, 10000, []),
        (801, 0.9, 0.9, True, 10000, ["boundary_difference"]),
        (-801, 0.9, 0.9, True, 10000, ["boundary_difference"]),
        (0, 0.499, 0.9, True, 10000, ["low_alignment_confidence"]),
        (0, 0.9, 0.099, True, 10000, ["low_alignment_confidence"]),
        (0, float("nan"), 0.9, True, 10000, ["low_alignment_confidence"]),
        (0, 0.9, 0.9, False, 10000, ["transcript_mismatch"]),
        (0, 0.9, 0.9, True, None, ["alignment_failed"]),
    ],
)
def test_ctc_boundary_flag_rule(offset, score, minimum, matched, aligned, reasons):
    from tools.corpus import boundary_review_reasons

    assert (
        boundary_review_reasons(
            10000 + offset,
            aligned,
            16000,
            alignment_confidence=score,
            minimum_word_confidence=minimum,
            transcript_matches=matched,
        )
        == reasons
    )


@pytest.mark.parametrize("change", ["flag", "reasons", "confidence", "transcript"])
def test_imported_ctc_flags_cannot_hide_review_conditions(change):
    from tools.corpus import label_errors

    item = row(group="replay")
    item.update(
        aligned_end_sample=13199,
        alignment_confidence=0.9,
        alignment_minimum_word_confidence=0.9,
        transcript_alignment_status="match",
        boundary_review_required=True,
        boundary_review_reasons=["boundary_difference"],
    )
    assert label_errors(item) == []
    if change == "flag":
        item["boundary_review_required"] = False
    elif change == "reasons":
        item["boundary_review_reasons"] = []
    elif change == "confidence":
        item["alignment_confidence"] = 0.49
    else:
        item["transcript_alignment_status"] = "mismatch"
    assert any("boundary review" in failure for failure in label_errors(item))


@pytest.mark.parametrize(
    "change",
    ["missing", "unknown", "method", "crosscheck", "parameters", "parameter_hash"],
)
def test_annotation_provenance_required(change):
    from tools.corpus import label_errors

    item = row(group="replay")
    item.update(
        last_voice_annotation="automatic",
        last_voice_method="energy",
        last_voice_crosscheck_method="aligned",
        last_voice_parameters={"window_ms": 10},
        last_voice_parameter_sha256="0" * 64,
    )
    if change == "missing":
        del item["last_voice_annotation"]
    elif change == "unknown":
        item["last_voice_annotation"] = "guessed"
    else:
        field = {
            "method": "last_voice_method",
            "crosscheck": "last_voice_crosscheck_method",
            "parameters": "last_voice_parameters",
            "parameter_hash": "last_voice_parameter_sha256",
        }[change]
        del item[field]
    assert any(
        "annotation" in error or "automatic" in error for error in label_errors(item)
    )


@pytest.mark.parametrize(
    "change",
    [
        "shape",
        "credit",
        "license_hash",
        "archive_hash",
        "member",
        "size",
        "license_text",
    ],
)
def test_source_records_are_validated(tmp_path, archive_bundle, change):
    from tools.corpus import check_records

    records = json.loads((tmp_path / "LICENSES.json").read_text())
    if change == "shape":
        records["sources"] = "invalid"
    else:
        key = {
            "credit": "credit",
            "license_hash": "license_sha256",
            "archive_hash": "archive_sha256",
            "member": "license_member",
            "size": "archive_size",
            "license_text": "license_text",
        }[change]
        records["sources"][0][key] = ""
    assert any("source record" in error for error in check_records([], records, []))


def test_fetch_checks_license_bytes_before_audio_write(tmp_path, archive_bundle):
    from tools import corpus

    item, _, _ = archive_bundle
    records = json.loads((tmp_path / "LICENSES.json").read_text())
    records["sources"][0]["license_text"] = "Different license"
    records["sources"][0]["license_sha256"] = hashlib.sha256(
        b"Different license"
    ).hexdigest()
    (tmp_path / "LICENSES.json").write_text(json.dumps(records))
    failures = corpus.fetch([item], tmp_path / "audio", cache=tmp_path / "cache")
    assert any("license member hash mismatch" in error for error in failures)
    assert not (tmp_path / "audio").exists()


@pytest.mark.parametrize("archive", [False, True])
def test_fetch_download_cap_stops_before_write(
    tmp_path, monkeypatch, archive_bundle, archive
):
    from tools import corpus

    item, _, data = archive_bundle
    if not archive:
        item["source_uri"] = "https://data.example/clip.wav"

        class Response(io.BytesIO):
            def geturl(self):
                return item["source_uri"]

        monkeypatch.setattr(corpus, "urlopen", lambda *a, **k: Response(data))
    failures = corpus.fetch(
        [item], tmp_path / "audio", cache=tmp_path / "cache", max_download_bytes=32
    )
    assert any("download size cap" in error for error in failures)
    assert not (tmp_path / "audio").exists()
    assert not list((tmp_path / "cache").glob("*.zip"))


def test_manual_boundary_keeps_automatic_review_flags():
    from tools.corpus import label_errors

    item = row(group="replay")
    item.update(
        last_voice_sample=10000,
        aligned_end_sample=10000,
        energy_minus_aligned_samples=801,
        alignment_confidence=0.9,
        alignment_minimum_word_confidence=0.9,
        transcript_alignment_status="match",
        boundary_review_required=True,
        boundary_review_reasons=["boundary_difference"],
    )
    assert label_errors(item) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("last_voice_method", "unknown"),
        (
            "last_voice_parameters",
            {"absolute_threshold": True, "relative_threshold": 0.08, "window_ms": 10},
        ),
        (
            "last_voice_parameters",
            {"absolute_threshold": 0.006, "relative_threshold": 0.08, "window_ms": 10},
        ),
        (
            "last_voice_parameters",
            {"absolute_threshold": 0.005, "relative_threshold": 0.09, "window_ms": 10},
        ),
        (
            "last_voice_parameters",
            {"absolute_threshold": 0.005, "relative_threshold": 0.08, "window_ms": 20},
        ),
        ("last_voice_parameter_sha256", "not-a-hash"),
        ("last_voice_crosscheck_method", "unknown"),
        ("alignment_model_sha256", "not-a-hash"),
        ("alignment_frame_samples", float("nan")),
        ("alignment_transcript", [1]),
    ],
)
@pytest.mark.parametrize("missing", [False, True])
def test_archive_provenance_is_required_and_typed(
    archive_bundle, field, value, missing
):
    from tools.corpus import label_errors

    item, _, _ = archive_bundle
    assert label_errors(item) == []
    if missing:
        del item[field]
    else:
        item[field] = value
    assert any("archive provenance" in error for error in label_errors(item))
