import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from test_public_tree import git

from tools.check_packaging import check_packaging, valid_client_requirement

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path("custom_components/assist_live")


@pytest.fixture
def packaged_root(tmp_path):
    shutil.copytree(
        ROOT / PACKAGE,
        tmp_path / PACKAGE,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ("hacs.json", "uv.lock", ".gitignore"):
        shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


def test_package_boundary():
    manifest = json.loads((ROOT / PACKAGE / "manifest.json").read_text())
    assert manifest["domain"] == "assist_live"
    assert manifest["config_flow"] is True
    assert not (ROOT / "ha-app").exists()
    assert "Apache" in (ROOT / "LICENSE").read_text()


def test_packaging_accepts_new_package_files(packaged_root):
    (packaged_root / PACKAGE / "new_helper.py").write_text("VALUE = 1\n")
    assert check_packaging(packaged_root) == []


def test_packaging_rejects_broken_package_import(packaged_root):
    (packaged_root / PACKAGE / "stt_fallback.py").write_text("raise ImportError\n")
    assert "package imports" in check_packaging(packaged_root)


@pytest.mark.parametrize(
    ("name", "data", "failure"),
    [
        ("brand/icon.png", b"Not a PNG.", "invalid brand image brand/icon.png"),
        (
            "brand/icon.png",
            b"\x89PNG\r\n\x1a\n" + b"\0" * (32 * 1024 - 7),
            "invalid brand image brand/icon.png",
        ),
        (
            "assist_live_test_agent/__init__.py",
            b"",
            "test agent package path assist_live_test_agent/__init__.py",
        ),
    ],
)
def test_packaging_rejects_unsafe_package_files(packaged_root, name, data, failure):
    path = packaged_root / PACKAGE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    assert failure in check_packaging(packaged_root)


def test_packaging_rejects_shipped_bytecode(packaged_root):
    path = packaged_root / PACKAGE / "stt.pyc"
    path.write_bytes(b"test-only")
    assert "package bytecode stt.pyc" in check_packaging(packaged_root)

    git(packaged_root, "init", "-b", "main")
    assert check_packaging(packaged_root) == []
    git(packaged_root, "add", "-f", str(path))
    assert "package bytecode stt.pyc" in check_packaging(packaged_root)


def test_packaging_rejects_bad_metadata(packaged_root):
    path = packaged_root / PACKAGE / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["version"] = "not-a-version"
    manifest["config_flow"] = False
    path.write_text(json.dumps(manifest))
    (packaged_root / "hacs.json").write_text('{"name": "", "homeassistant": "invalid"}')
    assert {
        "manifest version",
        "manifest config_flow",
        "HACS name",
        "HACS homeassistant",
    } <= set(check_packaging(packaged_root))


def test_packaging_rejects_missing_package(tmp_path):
    assert check_packaging(tmp_path)


@pytest.mark.parametrize(
    "requirements",
    [
        [],
        ["assist-live-client"],
        ["assist-live-client>=0.0.0"],
        ["assist-live-client==1.2.3"],
        ["./client"],
        ["git+https://example.com/client"],
        ["assist-live-client==0.0.0", "extra==1.0.0"],
    ],
)
def test_packaging_requires_locked_client(packaged_root, requirements):
    path = packaged_root / PACKAGE / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["requirements"] = requirements
    path.write_text(json.dumps(manifest))
    assert "manifest requirements" in check_packaging(packaged_root)


def test_packaging_rejects_git_client_source(packaged_root):
    path = packaged_root / "uv.lock"
    path.write_text(
        re.sub(
            r'(name = "assist-live-client"\nversion = "[^"]+"\n)source = [^\n]+',
            r'\1source = { git = "https://example.com/client" }',
            path.read_text(),
        )
    )
    assert "manifest requirements" in check_packaging(packaged_root)


def test_client_requirement_tracks_lock_version(tmp_path):
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="assist-live-client"\nversion="1.2.3"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    assert valid_client_requirement(tmp_path, ["assist-live-client==1.2.3"])
    assert not valid_client_requirement(tmp_path, ["assist-live-client==0.0.0"])


def test_release_archive_passes_its_extracted_packaging_check(tmp_path):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/release-artifacts.yml").read_text()
    )
    command = next(
        step["run"]
        for step in workflow["jobs"]["archive"]["steps"]
        if step.get("name") == "Build and check the archive without publication"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", command],
        cwd=ROOT,
        env={**os.environ, "RUNNER_TEMP": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"release_archive_packaging_failed\n{result.stdout}\n{result.stderr}"
    )
    extracted = tmp_path / "package"
    for name in ("brand/icon.png", "brand/icon@2x.png"):
        assert (extracted / PACKAGE / name).read_bytes() == (
            ROOT / PACKAGE / name
        ).read_bytes()
    (extracted / PACKAGE / "stt_fallback.py").unlink()
    assert "package imports" in check_packaging(extracted)
