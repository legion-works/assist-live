import hashlib
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


def run_checker(root, denylist, *args, env=None, history=("--history",)):
    denylist_args = ["--private-denylist", str(denylist)] if denylist else []
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            *history,
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


def test_history_range_checks_only_selected_commits(repository, denylist):
    git(repository, "commit", "--allow-empty", "-m", "Start")
    git(repository, "switch", "-c", "other")
    (repository / "data.txt").write_text("restricted-fixture-marker")
    git(repository, "add", "data.txt")
    git(repository, "commit", "-m", "Add private test data")
    git(repository, "switch", "main")
    git(repository, "commit", "--allow-empty", "-m", "Public change")

    outside = run_checker(
        repository, denylist, history=("--history-range", "HEAD^..HEAD")
    )
    assert outside.returncode == 0, outside.stdout or outside.stderr
    assert "commits=1" in outside.stdout

    inside = run_checker(
        repository, denylist, history=("--history-range", "main^..other")
    )
    assert inside.returncode == 1, inside.stdout or inside.stderr
    assert "blob commit=" in inside.stdout and "denylist=1" in inside.stdout
    assert "restricted-fixture-marker" not in inside.stdout

    (repository / "local.txt").write_text("restricted-fixture-marker")
    local = run_checker(
        repository, denylist, history=("--history-range", "HEAD^..HEAD")
    )
    assert local.returncode == 1
    assert "worktree" in local.stdout and "denylist=1" in local.stdout


@pytest.mark.parametrize("revision", ["missing..HEAD", "HEAD...HEAD", "..HEAD", "HEAD"])
def test_invalid_history_range_fails_closed(repository, denylist, revision):
    git(repository, "commit", "--allow-empty", "-m", "Start")
    result = run_checker(repository, denylist, history=("--history-range", revision))
    assert result.returncode == 1
    assert "FAIL public data input_or_scan_error=1" in result.stdout


def test_history_modes_are_mutually_exclusive(repository, denylist):
    result = run_checker(repository, denylist, "--history-range", "HEAD^..HEAD")
    assert result.returncode == 2
    assert "not allowed with argument --history" in result.stderr


def test_gitleaks_scans_only_selected_commits(repository, denylist):
    (repository / ".gitleaks.toml").write_text(
        '[[rules]]\nid = "fixture"\ndescription = "Synthetic test rule"\n'
        'regex = "synthetic-scanner-marker"\n'
    )
    git(repository, "commit", "--allow-empty", "-m", "Start")
    path = repository / "example.txt"
    fixture = "synthetic-scanner-marker"
    path.write_text(fixture)
    git(repository, "add", path.name)
    git(repository, "commit", "-m", "Add scanner fixture")
    path.unlink()
    git(repository, "add", "-u")
    git(repository, "commit", "-m", "Remove scanner fixture")

    outside = run_checker(
        repository, denylist, history=("--history-range", "HEAD^..HEAD")
    )
    assert outside.returncode == 0, outside.stdout or outside.stderr
    inside = run_checker(
        repository, denylist, history=("--history-range", "HEAD~2..HEAD^")
    )
    assert inside.returncode == 1, inside.stdout or inside.stderr
    assert "FAIL history secrets scanner_failure=1" in inside.stdout
    assert fixture not in inside.stdout + inside.stderr


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


@pytest.mark.parametrize("name", ["icon.png", "icon@2x.png"])
def test_public_tree_accepts_only_the_two_brand_png_paths(name):
    from tools.check_public_tree import scan_data

    png = b"\x89PNG\r\n\x1a\n"
    path = f"custom_components/assist_live/brand/{name}"
    assert not scan_data(png, (), set(), path)
    assert "binary" in scan_data(png, (), set(), f"docs/{name}")
    assert "binary" in scan_data(
        png, (), set(), "custom_components/assist_live/brand/logo.png"
    )


@pytest.mark.parametrize(
    "payload",
    [b"Not a PNG.", b"\x89PNG\r\n\x1a\n" + b"\0" * (32 * 1024 - 7)],
    ids=["renamed-text", "oversize"],
)
def test_public_tree_refuses_invalid_brand_png(payload):
    from tools.check_public_tree import scan_data

    path = "custom_components/assist_live/brand/icon.png"
    assert "brand_image" in scan_data(payload, (), set(), path)


def test_public_tree_still_scans_brand_png_for_private_text():
    from tools.check_public_tree import scan_data

    path = "custom_components/assist_live/brand/icon.png"
    marker = "restricted-fixture-marker"
    png = b"\x89PNG\r\n\x1a\n" + marker.encode()
    assert scan_data(png, [marker], set(), path) == {"denylist": 1}


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
