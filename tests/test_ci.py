import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from test_public_tree import denylist, repository  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = Path(".github/workflows")


def test_ha_plugin_is_active(request):
    assert request.config.pluginmanager.hasplugin("homeassistant")


@pytest.mark.parametrize("mode", ["unset", "valid", "missing", "hit"])
def test_task_privacy_branch(repository, denylist, tmp_path, mode):  # noqa: F811
    tool = repository / "tools/check_public_tree.py"
    tool.parent.mkdir()
    shutil.copy(ROOT / "tools/check_public_tree.py", tool)
    commands = yaml.safe_load((ROOT / "Taskfile.yml").read_text())["tasks"]["check"][
        "cmds"
    ]
    branch = next(
        cmd
        for cmd in commands
        if isinstance(cmd, str) and "check_public_tree.py" in cmd
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\nimport os,sys\n"
        "assert sys.argv[1:4] == ['run', '--locked', 'python']\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[4:]])\n"
    )
    uv.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if key != "PRIVATE_DENYLIST"}
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    if mode != "unset":
        env["PRIVATE_DENYLIST"] = str(
            denylist if mode != "missing" else tmp_path / "absent"
        )
    if mode == "hit":
        (repository / "example.txt").write_text("restricted-fixture-marker")
    result = subprocess.run(
        ["bash", "-e", "-c", branch],
        cwd=repository,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is (mode in ("unset", "valid")), result.stdout
    assert ("NOT CHECKED private denylist" in result.stdout) is (mode == "unset")
    if mode in ("unset", "valid"):
        assert "PASS history secrets" in result.stdout
        assert "PASS public data matches=0" in result.stdout


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", "success"])
def test_completion_requires_each_success(result):
    from tools.check_ci import results_pass

    jobs = {"lint", "typecheck", "unit", "ha", "hassfest", "hacs", "secrets"}
    needs: dict[str, dict[str, object]] = {job: {"result": "success"} for job in jobs}
    outputs = {"minimum": "success", "current": "success", "compatibility": "success"}
    needs["ha"]["outputs"] = outputs
    assert results_pass(needs)
    for job in jobs:
        changed = {**needs, job: {**needs[job], "result": result}}
        assert results_pass(changed) is (result == "success")
    for member in outputs:
        changed = {
            **needs,
            "ha": {"result": "success", "outputs": {**outputs, member: result}},
        }
        assert results_pass(changed) is (result == "success")
    assert not results_pass({})
    assert not results_pass({key: value for key, value in needs.items() if key != "ha"})
    assert not results_pass({**needs, "ha": {"result": "success"}})


def test_ci_reports_incomplete_results(capsys):
    from tools.check_ci import main

    needs: dict[str, dict[str, object]] = {
        job: {"result": "success"}
        for job in ("lint", "typecheck", "unit", "ha", "hassfest", "hacs", "secrets")
    }
    needs["ha"]["outputs"] = {"minimum": "success", "current": "success"}
    assert main(["--results", json.dumps(needs)]) == 1
    assert capsys.readouterr().out == "FAIL CI incomplete CI results\n"


@pytest.mark.parametrize(
    ("name", "job"),
    [("release-please", "release-please"), ("release-artifacts", "archive")],
)
def test_release_workflows_require_approval(name, job):
    workflow = yaml.safe_load((ROOT / WORKFLOWS / f"{name}.yml").read_text())
    guard = workflow["jobs"][job]["steps"][0]
    assert guard["env"] == {"APPROVED": "${{ vars.ASSIST_LIVE_RELEASES_APPROVED }}"}
    for value in ("", "false", "true"):
        result = subprocess.run(
            ["bash", "-e", "-c", guard["run"]],
            env={**os.environ, "APPROVED": value},
            capture_output=True,
            check=False,
        )
        assert (result.returncode == 0) is (value == "true")


def test_release_publisher_requires_archive_check_in_same_run():
    from tools.check_ci import check_release_publisher

    check_release_publisher(ROOT)


@pytest.fixture
def release_root(tmp_path):
    workflows = tmp_path / WORKFLOWS
    workflows.mkdir(parents=True)
    for name in ("release-please.yml", "release-artifacts.yml"):
        shutil.copyfile(ROOT / WORKFLOWS / name, workflows / name)
    return tmp_path


def test_release_check_runs_without_the_ci_workflow(release_root, monkeypatch, capsys):
    from tools import check_ci

    monkeypatch.setattr(check_ci, "__file__", str(release_root / "tools/check_ci.py"))
    assert check_ci.main([]) == 0
    assert capsys.readouterr().out == "PASS CI\n"


@pytest.mark.parametrize(
    ("file", "job", "field", "value"),
    [
        ("release-please.yml", "release-please", "needs", None),
        ("release-please.yml", "release-please", "if", "${{ always() }}"),
        ("release-artifacts.yml", "archive", "continue-on-error", True),
        ("release-please.yml", "archive", "if", None),
    ],
)
def test_release_publisher_rejects_dependency_bypass(
    release_root, file, job, field, value
):
    from tools.check_ci import check_release_publisher

    path = release_root / WORKFLOWS / file
    workflow = yaml.safe_load(path.read_text())
    job_config = workflow["jobs"][job]
    if value is None:
        job_config.pop(field)
    else:
        job_config[field] = value
    path.write_text(yaml.safe_dump(workflow))
    with pytest.raises(ValueError, match="^release publisher dependency$"):
        check_release_publisher(release_root)
