import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools/check_public_tree.py"


def git(root, *args):
    return (
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "Project tests",
                "GIT_COMMITTER_NAME": "Project tests",
                "GIT_AUTHOR_EMAIL": "tests@example.invalid",
                "GIT_COMMITTER_EMAIL": "tests@example.invalid",
            },
        )
        .stdout.decode()
        .strip()
    )


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "public"
    root.mkdir()
    git(root, "init", "-b", "main")
    return root


@pytest.fixture
def denylist(tmp_path):
    path = tmp_path / "denylist.txt"
    path.write_text("# Synthetic fixture only\nrestricted-fixture-marker\n")
    return path


def run_checker(root, denylist, *args, env=None):
    denylist_args = ["--private-denylist", str(denylist)] if denylist else []
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--history",
            "--root",
            str(root),
            *denylist_args,
            *args,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_checker_without_private_denylist_reports_unchecked(repository):
    result = run_checker(repository, None)
    assert result.returncode == 0, result.stdout
    assert result.stdout.count("NOT CHECKED private denylist") == 1
    assert "PASS public data" in result.stdout


def test_checker_with_private_denylist_runs_full_scan(repository, denylist):
    result = run_checker(repository, denylist)
    assert result.returncode == 0, result.stdout
    assert "NOT CHECKED private denylist" not in result.stdout
    assert "PASS public data" in result.stdout


def test_parent_repository_is_not_accepted(tmp_path):
    from tools.check_public_tree import require_own_toplevel

    with pytest.raises(ValueError):
        require_own_toplevel(tmp_path / "nested", toplevel=tmp_path)


def test_nested_worktree_is_rejected(repository, denylist):
    nested = repository / "nested"
    nested.mkdir()
    result = run_checker(nested, denylist)
    assert result.returncode == 1
    assert "FAIL own Git root" in result.stdout
    assert str(nested) not in result.stdout


def test_empty_history_is_explicit(repository, denylist):
    (repository / "README.md").write_text("Public text.\n")
    result = run_checker(repository, denylist)
    assert result.returncode == 0, result.stdout
    assert "PASS own Git root" in result.stdout
    assert "PASS history secrets" in result.stdout
    assert "commits=0" in result.stdout
    assert "PASS public data" in result.stdout


def test_removed_private_text_in_history_fails(repository, denylist):
    path = repository / "data.txt"
    path.write_text("RESTRICTED-FIXTURE-MARKER\n")
    git(repository, "add", "data.txt")
    git(repository, "commit", "-m", "Add synthetic fixture")
    bad_commit = git(repository, "rev-parse", "HEAD")
    path.unlink()
    git(repository, "add", "-u")
    git(repository, "commit", "-m", "Remove synthetic fixture")
    result = run_checker(repository, denylist)
    assert result.returncode == 1
    assert "FAIL public data" in result.stdout
    assert bad_commit in result.stdout
    assert "restricted-fixture-marker" not in result.stdout.lower()
    assert "denylist=1" in result.stdout


def test_private_commit_metadata_fails(repository, denylist):
    git(repository, "commit", "--allow-empty", "-m", "restricted-fixture-marker")
    result = run_checker(repository, denylist)
    assert result.returncode == 1
    assert "metadata" in result.stdout
    assert "restricted-fixture-marker" not in result.stdout


@pytest.mark.parametrize(
    "payload",
    [
        "10" + ".0.0.1",
        "172" + ".16.0.1",
        "192" + ".168.0.1",
        "169" + ".254.0.1",
        "100" + ".64.0.1",
        "fc" + "00::1",
        "fd" + "12:3456::1",
        "/" + "home" + "/fixture/data",
        "/" + "Users" + "/fixture/data",
        "C:" + "\\Users\\fixture\\data",
    ],
)
def test_private_data_patterns(payload):
    from tools.check_public_tree import scan_data

    assert scan_data(payload.encode(), (), set(), "text.txt")


def test_documentation_addresses_are_permitted():
    from tools.check_public_tree import scan_data

    text = "192.0.2.1 198.51.100.8 203.0.113.9 127.0.0.1"
    assert not scan_data(text.encode(), (), set(), "text.txt")


@pytest.mark.parametrize(
    "payload,name", [(b"RIFF0000WAVEdata", "sample"), (b"audio", "test.mp3")]
)
def test_audio_requires_explicit_hash(payload, name):
    from tools.check_public_tree import scan_data

    assert "audio" in scan_data(payload, (), set(), name)
    digest = hashlib.sha256(payload).hexdigest()
    assert not scan_data(payload, (), {digest}, name)


def test_unknown_binary_is_not_licensed_audio():
    from tools.check_public_tree import scan_data

    data = b"\x00\x01unknown"
    assert "binary" in scan_data(
        data, (), {hashlib.sha256(data).hexdigest()}, "file.bin"
    )


@pytest.mark.parametrize("data", [b"\x7fdata", "\u0080data".encode()])
def test_nonprinting_binary_is_rejected(data):
    from tools.check_public_tree import scan_data

    assert "binary" in scan_data(data, (), set(), "unknown")


@pytest.mark.parametrize(
    "data", [b"\xff\xf1aac", b".snd0000", b"caff0000", b"#!AMR\n0000"]
)
def test_audio_magic_without_extension_needs_a_hash(data):
    from tools.check_public_tree import scan_data

    assert "audio" in scan_data(data, (), set(), "sample")
    assert not scan_data(data, (), {hashlib.sha256(data).hexdigest()}, "sample")


def test_denylist_mutation_fails_without_disclosure(repository, denylist):
    result = run_checker(repository, denylist)
    assert result.returncode == 0
    path = repository / "temporary.txt"
    path.write_text("restricted-fixture-marker")
    result = run_checker(repository, denylist)
    assert result.returncode == 1
    assert "denylist=1" in result.stdout
    assert "restricted-fixture-marker" not in result.stdout
    path.unlink()
    assert run_checker(repository, denylist).returncode == 0


def test_missing_gitleaks_fails(repository, denylist, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(shutil.which("git"))
    result = run_checker(repository, denylist, env={**os.environ, "PATH": str(bin_dir)})
    assert result.returncode == 1
    assert "FAIL history secrets" in result.stdout


def test_symlink_is_not_followed(repository, denylist, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("restricted-fixture-marker")
    (repository / "link").symlink_to(target)
    result = run_checker(repository, denylist)
    assert result.returncode == 1
    assert "file_type=1" in result.stdout


def test_ignored_worktree_data_is_not_published(repository, denylist):
    (repository / ".gitignore").write_text("ignored.txt\n")
    (repository / "ignored.txt").write_text("restricted-fixture-marker")
    assert run_checker(repository, denylist).returncode == 0


@pytest.mark.parametrize(
    "change", ["document", "license", "manifest", "requirement", "privacy"]
)
def test_bootstrap_rejects_invalid_boundary(tmp_path, change):
    from tools.check_bootstrap import check_bootstrap

    root = tmp_path / "bootstrap"
    shutil.copytree(
        ROOT,
        root,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"
        ),
    )
    if change == "document":
        (root / "SECURITY.md").unlink()
    elif change == "license":
        (root / "LICENSE").write_text("Unknown license")
    elif change in {"manifest", "requirement"}:
        manifest_path = root / "custom_components/assist_live/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        if change == "manifest":
            manifest.pop("config_flow")
        else:
            manifest["requirements"] = ["unpublished-client>=0"]
        manifest_path.write_text(json.dumps(manifest))
    else:
        (root / "README.md").write_text("192" + ".168.0.1")
    assert check_bootstrap(root)


def test_bootstrap_accepts_exact_release_requirement(tmp_path):
    from tools.check_bootstrap import check_bootstrap

    root = tmp_path / "bootstrap"
    shutil.copytree(
        ROOT,
        root,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"
        ),
    )
    path = root / "custom_components/assist_live/manifest.json"
    manifest = json.loads(path.read_text())
    manifest["requirements"] = ["assist-live-client==1.2.3"]
    path.write_text(json.dumps(manifest))
    assert check_bootstrap(root) == []


@pytest.fixture
def bootstrap_copy(tmp_path):
    root = tmp_path / "bootstrap"
    shutil.copytree(
        ROOT,
        root,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
        ),
    )
    return root


def test_shipped_hassfest_metadata_shape():
    package = ROOT / "custom_components/assist_live"
    manifest = json.loads((package / "manifest.json").read_text())
    assert list(manifest) == [
        "domain",
        "name",
        "codeowners",
        "config_flow",
        "documentation",
        "integration_type",
        "iot_class",
        "issue_tracker",
        "requirements",
        "version",
    ]
    for name in ("strings.json", "translations/en.json"):
        strings = json.loads((package / name).read_text())
        assert strings["config"]["step"]["user"]["title"]
        assert strings["config"]["step"]["user"]["description"]


@pytest.mark.parametrize("change", ["manifest_order", "step", "title", "description"])
def test_bootstrap_rejects_hassfest_metadata_regressions(bootstrap_copy, change):
    from tools.check_bootstrap import check_bootstrap

    package = bootstrap_copy / "custom_components/assist_live"
    if change == "manifest_order":
        path = package / "manifest.json"
        manifest = json.loads(path.read_text())
        path.write_text(json.dumps(dict(reversed(list(manifest.items())))))
        expected = "manifest key order"
    else:
        for name in ("strings.json", "translations/en.json"):
            path = package / name
            strings = json.loads(path.read_text())
            if change == "step":
                strings["config"].pop("step", None)
            else:
                user = strings["config"].get("step", {}).get("user", {})
                user.pop(change, None)
            path.write_text(json.dumps(strings))
        expected = "config flow steps"
    assert expected in check_bootstrap(bootstrap_copy)


@pytest.mark.parametrize("suffix", [".", ".txt"])
@pytest.mark.parametrize(
    "address",
    [
        "10" + ".1.2.3",
        "172" + ".20.0.1",
        "192" + ".168.0.10",
        "169" + ".254.0.1",
        "100" + ".64.0.1",
    ],
)
def test_private_ipv4_before_period_is_rejected(repository, denylist, address, suffix):
    from tools.check_public_tree import scan_data

    payload = "host " + address + suffix
    assert scan_data(payload.encode(), (), set(), "text.txt")["network"] == 1
    (repository / "text.txt").write_text(payload)
    result = run_checker(repository, denylist)
    assert result.returncode == 1
    assert "network=1" in result.stdout
    assert address not in result.stdout


@pytest.mark.parametrize(
    "payload", ["2026.10.3", "1.10.0.0", "10" + ".1.2.3.4", "10" + ".1.2.3abc"]
)
def test_versions_and_digit_group_continuations_are_not_private_addresses(payload):
    from tools.check_public_tree import scan_data

    assert not scan_data(payload.encode(), (), set(), "text.txt")


def test_bootstrap_rejects_private_address_before_period(bootstrap_copy):
    from tools.check_bootstrap import check_bootstrap

    (bootstrap_copy / "README.md").write_text("host 10" + ".1.2.3. done")
    assert "public data README.md" in check_bootstrap(bootstrap_copy)


@pytest.mark.parametrize(
    "payload",
    [
        "/" + "home" + "/fixture",
        "files live in /" + "home" + "/fixture",
        "/" + "Users" + "/fixture",
        "C:" + "\\Users\\fixture",
    ],
)
def test_private_path_without_trailing_separator_is_rejected(payload):
    from tools.check_public_tree import scan_data

    assert scan_data(payload.encode(), (), set(), "text.txt")["private_path"] == 1


@pytest.mark.parametrize(
    "url",
    ["https://github.com/users/balloob", "https://example.com/home/user"],
)
def test_public_urls_are_not_private_user_paths(url):
    from tools.check_public_tree import scan_data

    assert not scan_data(url.encode(), (), set(), "text.txt")


def test_bootstrap_rejects_untested_ha_minimum(bootstrap_copy):
    from tools.check_bootstrap import check_bootstrap

    path = bootstrap_copy / "hacs.json"
    hacs = json.loads(path.read_text())
    hacs["homeassistant"] = "2026.10.0"
    path.write_text(json.dumps(hacs))
    assert "HACS metadata" in check_bootstrap(bootstrap_copy)


def test_repository_inside_another_repository_is_rejected(repository, denylist):
    from tools.check_public_tree import require_own_toplevel

    nested = repository / "nested"
    nested.mkdir()
    git(nested, "init", "-b", "main")
    with pytest.raises(ValueError):
        require_own_toplevel(nested)
    result = run_checker(nested, denylist)
    assert result.returncode == 1
    assert "FAIL own Git root" in result.stdout
