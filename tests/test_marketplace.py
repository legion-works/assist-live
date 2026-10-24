import copy
import json
import re
import shutil
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_unreleased_marketplace_is_pending():
    from tools.check_marketplace import classify

    assert classify(released=False, accepted=None) == "pending_release"


@pytest.mark.parametrize(
    ("released", "accepted", "expected"),
    [
        (False, True, "pending_release"),
        (True, True, "accepted"),
        (True, False, "rejected"),
    ],
)
def test_classification_never_claims_acceptance_before_release(
    released, accepted, expected
):
    from tools.check_marketplace import classify

    assert classify(released=released, accepted=accepted) == expected


def test_released_without_validation_is_not_a_rejection():
    from tools.check_marketplace import classify

    with pytest.raises(ValueError, match="validation_required"):
        classify(released=True, accepted=None)


@pytest.fixture
def record():
    text = (ROOT / "docs/research/q3-marketplace.md").read_text()
    match = re.search(r"```json\n(.*?)\n```", text, re.S)
    assert match is not None
    return json.loads(match[1])


def test_record_status_is_offline_and_does_not_mean_acceptance(monkeypatch, capsys):
    from tools import check_marketplace

    def no_network(*args, **kwargs):
        pytest.fail("offline status check used the network")

    monkeypatch.setattr(check_marketplace, "urlopen", no_network)
    assert (
        check_marketplace.main(
            [
                "--minimum",
                "2026.11",
                "--report",
                str(ROOT / "docs/research/q3-marketplace.md"),
                "--record-status",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == (
        "STATUS Q3 pending_release (recorded observation, not acceptance)\n"
        "PASS Q3 status recorded\n"
    )


def test_require_accepted_fails_today_with_named_reason(capsys):
    from tools.check_marketplace import main

    assert main(["--minimum", "2026.11", "--require-accepted"]) == 1
    assert "FAIL Q3 pending_release: HA 2026.11 has no recorded stable release" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("status", "accepted", "status"),
        ("release_version", "2026.11.0", "release_version"),
        ("checked_at", "not-a-date", "checked_at"),
        ("sources", {}, "sources"),
        ("rule", {}, "rule"),
        ("validator", {}, "validator"),
        ("validation", {}, "validation"),
    ],
)
def test_incomplete_or_contradictory_records_fail(record, key, value, reason):
    from tools.check_marketplace import validate_record

    record[key] = value
    assert any(reason in error for error in validate_record(record, "2026.11"))


def test_release_source_disagreement_cannot_record_pending(record):
    from tools.check_marketplace import validate_record

    record["sources"]["pypi"]["target_releases"] = ["2026.11.0"]
    assert "release_sources: disagree or require released validation" in (
        validate_record(record, "2026.11")
    )


def test_preview_versions_do_not_count_as_a_stable_release():
    from tools.check_marketplace import stable_versions

    assert stable_versions(
        ["2026.10.3", "2026.11.0b0", "2026.11.0rc1", "2026.11.0.dev0", "v2026.11.0"]
    ) == ["2026.10.3", "2026.11.0"]


def test_released_status_needs_both_marketplace_validation_records(record):
    from tools.check_marketplace import validate_record

    record["status"] = "accepted"
    record["release_version"] = "2026.11.0"
    for source in ("pypi", "github"):
        record["sources"][source]["target_releases"] = ["2026.11.0"]
    record["sources"]["release_notes"]["http_status"] = 200
    assert any("validation" in error for error in validate_record(record, "2026.11"))


@pytest.mark.parametrize("text", ["no JSON", "```json\n[]\n```", "```json\n{\n```"])
def test_bad_report_fails_without_a_traceback(tmp_path, capsys, text):
    from tools.check_marketplace import main

    report = tmp_path / "report.md"
    report.write_text(text)
    assert main(["--report", str(report), "--record-status"]) == 1
    assert capsys.readouterr().out.startswith("FAIL Q3 report:")


def test_unofficial_rule_source_cannot_support_a_record(record):
    from tools.check_marketplace import validate_record

    record["rule"]["url"] = "https://example.invalid/marketplace"
    assert any("rule" in error for error in validate_record(record, "2026.11"))


def test_live_mode_is_explicit_and_cannot_replace_acceptance(
    record, monkeypatch, capsys
):
    from tools import check_marketplace

    snapshot = copy.deepcopy(record)
    monkeypatch.setattr(check_marketplace, "fetch_live", lambda minimum: snapshot)
    assert check_marketplace.main(["--live", "--require-accepted"]) == 1
    assert "FAIL Q3 pending_release" in capsys.readouterr().out


def test_live_probe_reads_official_metadata_and_pins_core(monkeypatch):
    from tools import check_marketplace as probe

    sha = "a" * 40
    responses = {
        probe.SOURCES["pypi"]: (
            200,
            json.dumps(
                {
                    "releases": {
                        "2026.10.4": [{}],
                        "2026.11.0b0": [{}],
                        "2026.11.0": [],
                    }
                }
            ).encode(),
        ),
        probe.SOURCES["github"]: (
            200,
            json.dumps(
                [
                    {"tag_name": "2026.10.4", "draft": False, "prerelease": False},
                    {"tag_name": "2026.11.0", "draft": True, "prerelease": False},
                ]
            ).encode(),
        ),
        probe.SOURCES["release_notes"]: (404, b"not found"),
        probe.RULE_URL: (
            200,
            (
                b"The Marketplace also refuses repositories with <a>apps</a> for "
                b"<a>Home Assistant OS</a>."
            ),
        ),
        "https://api.github.com/repos/home-assistant/core/commits/dev": (
            200,
            json.dumps(
                {
                    "sha": sha,
                    "commit": {
                        "committer": {
                            "date": "2026-10-24T00:00:00Z",
                        }
                    },
                }
            ).encode(),
        ),
        "https://api.github.com/repos/home-assistant/core/contents/"
        f"homeassistant/components/marketplace/repositories?ref={sha}": (
            200,
            json.dumps(
                [
                    {
                        "name": "integration.py",
                        "download_url": f"https://raw.githubusercontent.com/home-assistant/core/{sha}/"
                        "homeassistant/components/marketplace/repositories/integration.py",
                    }
                ]
            ).encode(),
        ),
        f"https://raw.githubusercontent.com/home-assistant/core/{sha}/"
        "homeassistant/components/marketplace/repositories/integration.py": (
            200,
            b'if "repository.yaml" in self.treefiles:\n    raise AppRepositoryError()',
        ),
    }
    monkeypatch.setattr(probe, "fetch", lambda url: responses[url])
    result = probe.fetch_live("2026.11")
    assert result["sources"]["pypi"]["target_releases"] == []
    assert result["sources"]["github"]["latest_stable"] == "2026.10.4"
    assert result["validator"]["commit"] == sha
    assert probe.validate_record(result, "2026.11") == []


def test_local_app_fixture_is_not_marketplace_acceptance(tmp_path, capsys):
    from tools.check_marketplace import main

    repository = tmp_path / "app-repository"
    app = repository / "probe_app"
    app.mkdir(parents=True)
    (repository / "repository.yaml").write_text("name: Marketplace refusal fixture\n")
    (app / "config.yaml").write_text(
        "name: Refusal fixture\nversion: 0.0.0\nslug: refusal_fixture\n"
        "description: Synthetic app metadata\narch:\n  - amd64\n"
    )
    assert (repository / "repository.yaml").is_file()
    assert (app / "config.yaml").is_file()
    assert not (repository / "custom_components").exists()
    mixed = tmp_path / "mixed-repository"
    shutil.copytree(repository, mixed)
    shutil.copytree(ROOT / "custom_components", mixed / "custom_components")
    assert (mixed / "repository.yaml").is_file()
    assert (mixed / "custom_components/assist_live/manifest.json").is_file()
    assert main(["--require-accepted"]) == 1
    assert "FAIL Q3 pending_release" in capsys.readouterr().out


@pytest.fixture
def released_record(record):
    record["status"] = "accepted"
    record["release_version"] = "2026.11.0"
    record["validator"]["commit"] = "b" * 40
    record["validator"]["tag"] = "2026.11.0"
    record["validator"]["source_url"] = (
        "https://github.com/home-assistant/core/blob/2026.11.0/"
        "homeassistant/components/marketplace/repositories/integration.py"
    )
    record["validator"]["release_version"] = "2026.11.0"
    for source in ("pypi", "github"):
        record["sources"][source]["target_releases"] = ["2026.11.0"]
        record["sources"][source]["latest_stable"] = "2026.11.0"
    record["sources"]["release_notes"]["http_status"] = 200
    record["validation"] = {
        name: {
            "validator": "marketplace_install",
            "release_version": "2026.11.0",
            "checked_at": "2026-11-04",
            "outcome": outcome,
            "source_record": f"synthetic-test-log:{name}",
        }
        for name, outcome in [
            ("integration", "accepted"),
            ("app_fixture", "rejected"),
            ("mixed_fixture", "accepted"),
        ]
    }
    return record


def test_released_record_requires_marketplace_not_hacs_results(released_record):
    from tools.check_marketplace import validate_record

    assert validate_record(released_record, "2026.11") == []
    released_record["validation"]["integration"]["validator"] = "hacs"
    assert any(
        "validation.integration" in error
        for error in validate_record(released_record, "2026.11")
    )


@pytest.mark.parametrize("mutation", ["section", "steps_1_to_4", "acceptance_command"])
def test_pending_report_requires_complete_check_again_procedure(
    mutation, tmp_path, capsys
):
    from tools.check_marketplace import main

    original = (ROOT / "docs/research/q3-marketplace.md").read_text()
    text = original
    if mutation == "section":
        text = re.sub(
            r"## Check-again condition and procedure\n.*?(?=## Recorded source data)",
            "",
            text,
            flags=re.S,
        )
    elif mutation == "steps_1_to_4":
        text = re.sub(r"\n1\. .*?(?=\n5\. )", "\n", text, flags=re.S)
    else:
        text = text.replace("--require-accepted", "--record-status")
    assert text != original
    report = tmp_path / "report.md"
    report.write_text(text)
    assert main(["--record-status", "--report", str(report)]) == 1
    assert "FAIL Q3 procedure:" in capsys.readouterr().out


def test_complete_released_install_record_passes_acceptance_gate(
    released_record, tmp_path, capsys
):
    from tools.check_marketplace import main

    report = tmp_path / "report.md"
    report.write_text("```json\n" + json.dumps(released_record) + "\n```\n")
    assert main(["--require-accepted", "--report", str(report)]) == 0
    assert capsys.readouterr().out == "PASS Q3 accepted\n"


@pytest.mark.parametrize("source", ["pypi", "github"])
def test_stale_latest_stable_cannot_support_a_released_record(
    released_record, source, tmp_path, capsys
):
    from tools.check_marketplace import main

    released_record["sources"][source]["latest_stable"] = "2026.10.4"
    report = tmp_path / "report.md"
    report.write_text("```json\n" + json.dumps(released_record) + "\n```\n")
    assert main(["--require-accepted", "--report", str(report)]) == 1
    assert f"FAIL Q3 sources.{source}: latest_stable precedes release_version" in (
        capsys.readouterr().out
    )


def test_latest_stable_comparison_is_numeric(released_record):
    from tools.check_marketplace import validate_record

    released_record["release_version"] = "2026.11.2"
    released_record["validator"]["tag"] = "2026.11.2"
    released_record["validator"]["release_version"] = "2026.11.2"
    released_record["validator"]["source_url"] = released_record["validator"][
        "source_url"
    ].replace("2026.11.0", "2026.11.2")
    for source in ("pypi", "github"):
        released_record["sources"][source]["latest_stable"] = "2026.11.10"
        released_record["sources"][source]["target_releases"] = [
            "2026.11.2",
            "2026.11.10",
        ]
    for result in released_record["validation"].values():
        result["release_version"] = "2026.11.2"
    assert validate_record(released_record, "2026.11") == []


@pytest.mark.parametrize(
    "tag", [None, "dev", "2026.11.0b0", "2026.11.0rc1", "2026.11.1"]
)
def test_nonrelease_validator_tag_cannot_support_acceptance(
    released_record, tag, tmp_path, capsys
):
    from tools.check_marketplace import main

    released_record["validator"]["tag"] = tag
    report = tmp_path / "report.md"
    report.write_text("```json\n" + json.dumps(released_record) + "\n```\n")
    assert main(["--require-accepted", "--report", str(report)]) == 1
    assert "FAIL Q3 validator: tag must match the stable release_version" in (
        capsys.readouterr().out
    )


def test_dev_commit_url_with_release_label_cannot_support_acceptance(
    released_record, tmp_path, capsys
):
    from tools.check_marketplace import main

    released_record["validator"]["source_url"] = (
        "https://github.com/home-assistant/core/blob/"
        + released_record["validator"]["commit"]
        + "/homeassistant/components/marketplace/repositories/integration.py"
    )
    report = tmp_path / "report.md"
    report.write_text("```json\n" + json.dumps(released_record) + "\n```\n")
    assert main(["--require-accepted", "--report", str(report)]) == 1
    assert "FAIL Q3 validator:" in capsys.readouterr().out


@pytest.mark.parametrize("variable", ["GITHUB_TOKEN", "GH_TOKEN"])
def test_github_api_uses_environment_token_without_sending_it_to_other_sources(
    variable, monkeypatch, capsys
):
    from tools import check_marketplace as probe

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv(variable, "test-token")
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"test response"

    def opener(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr(probe, "urlopen", opener)
    assert probe.fetch(probe.SOURCES["github"]) == (200, b"test response")
    assert requests[-1].get_header("Authorization") == "Bearer test-token"
    for url in (probe.SOURCES["pypi"], probe.RULE_URL):
        probe.fetch(url)
        assert requests[-1].get_header("Authorization") is None
    assert capsys.readouterr().out == ""


def test_github_rate_limit_has_named_token_guidance(monkeypatch, capsys):
    from tools import check_marketplace as probe

    def opener(request, timeout):
        headers = Message()
        headers["X-RateLimit-Remaining"] = "0"
        raise HTTPError(
            request.full_url,
            403,
            "rate limit exceeded",
            headers,
            None,
        )

    def live(minimum):
        probe.fetch(probe.SOURCES["github"])

    monkeypatch.setattr(probe, "urlopen", opener)
    monkeypatch.setattr(probe, "fetch_live", live)
    assert probe.main(["--live", "--record-status"]) == 1
    assert capsys.readouterr().out == (
        "FAIL Q3 report: github_rate_limit: set GITHUB_TOKEN or GH_TOKEN "
        "and retry the explicit --live probe\n"
    )


def test_github_request_errors_do_not_print_authorization(monkeypatch, capsys):
    from tools import check_marketplace as probe

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    def opener(request, timeout):
        raise ValueError("Invalid header value b'Bearer test-token'")

    def live(minimum):
        probe.fetch(probe.SOURCES["github"])

    monkeypatch.setattr(probe, "urlopen", opener)
    monkeypatch.setattr(probe, "fetch_live", live)
    assert probe.main(["--live", "--record-status"]) == 1
    assert capsys.readouterr().out == (
        "FAIL Q3 report: github_request: request rejected; "
        "check GITHUB_TOKEN or GH_TOKEN\n"
    )
