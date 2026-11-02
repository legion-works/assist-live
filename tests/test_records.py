import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/check_records.py"


@pytest.mark.parametrize(
    "content,passed,line",
    [
        ("one\ntwo\nthree\nfour\n", True, None),
        ("one\ninserted\ntwo\nthree\n", False, "2"),
        ("one\nchanged\nthree\n", False, "2"),
        ("one\nthree\n", False, "2"),
        (None, False, "1"),
    ],
)
@pytest.mark.parametrize("mode", ["explicit", "local", "push"])
def test_records_only_accept_end_of_file_appends(
    tmp_path, content, passed, line, mode, monkeypatch
):
    def git(*args):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.invalid",
                *args,
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-b", "main")
    record = tmp_path / "docs/verification.md"
    record.parent.mkdir()
    record.write_text("one\ntwo\nthree\n")
    git("add", ".")
    git("commit", "-m", "test fixture")
    base = git("rev-parse", "HEAD")
    if content is None:
        record.unlink()
    else:
        record.write_text(content)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    if mode == "push":
        git("add", ".")
        git("commit", "-m", "test change")
        event = tmp_path / "event.json"
        event.write_text(json.dumps({"before": base}))
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path),
            *(["--base", base] if mode == "explicit" else []),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if passed else 1), result.stdout + result.stderr
    if not passed:
        assert f"docs/verification.md:{line}" in result.stdout
