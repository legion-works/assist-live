import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_RUN_EXEMPTIONS = {
    "lint": {"Install actionlint", "Check source build compilers"},
    "typecheck": {"Check source build compilers"},
    "unit": {"Install Gitleaks", "Install Node", "Check source build compilers"},
    "ha": {
        "Install Gitleaks",
        "Install Node",
        "Check source build compilers",
        "result",
    },
}


def test_ci_has_required_jobs():
    from tools.check_ci import check_jobs

    root = Path(__file__).resolve().parents[1]
    assert check_jobs(root / ".github/workflows/ci.yml") == {
        "lint",
        "typecheck",
        "unit",
        "ha",
        "hassfest",
        "hacs",
        "secrets",
        "ci-complete",
    }


def test_ci_commands_match_taskfile():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    taskfile = yaml.safe_load((ROOT / "Taskfile.yml").read_text())
    task_commands = {
        " ".join(command.split())
        for task in taskfile["tasks"].values()
        for command in task.get("cmds", [])
        if isinstance(command, str)
    }
    task_ha = taskfile["tasks"]["ha-minimum"]["cmds"][0]
    ha_task_args = shlex.split(task_ha)
    for job_name, exemptions in CI_RUN_EXEMPTIONS.items():
        substantive_commands = 0
        for step in workflow["jobs"][job_name]["steps"]:
            run = step.get("run")
            if not run:
                continue
            step_key = step.get("name") or step.get("id")
            if step_key in exemptions:
                continue
            parts = [part.strip() for part in run.split("&&") if part.strip()]
            assert parts, f"CI {job_name} has an empty run step: {step_key}"
            for part in parts:
                try:
                    command_args = shlex.split(part)
                except ValueError as error:
                    raise AssertionError(
                        f"CI {job_name} command is invalid: {part} ({error})"
                    ) from error
                if (
                    job_name == "ha"
                    and step_key == "Test all contracts on the exact HA version"
                ):
                    ha_version = "homeassistant==$HA_VERSION"
                    plugin_version = (
                        "pytest-homeassistant-custom-component==$PLUGIN_VERSION"
                    )
                    assert (
                        ha_version in command_args and plugin_version in command_args
                    ), f"CI {job_name} command has no Taskfile match: {part}"
                    command_args[command_args.index(ha_version)] = (
                        "homeassistant==2026.10.1"
                    )
                    command_args[command_args.index(plugin_version)] = (
                        "pytest-homeassistant-custom-component==0.13.372"
                    )
                    assert command_args == ha_task_args, (
                        f"CI {job_name} command has no Taskfile match: {part}"
                    )
                else:
                    normalized = shlex.join(command_args)
                    assert normalized in task_commands, (
                        f"CI {job_name} command has no Taskfile match: {part}"
                    )
                if command_args != ["uv", "sync", "--locked"]:
                    substantive_commands += 1
        assert substantive_commands > 0, (
            f"CI {job_name} has no Taskfile commands beyond uv sync"
        )


@pytest.mark.parametrize("job_name", ["unit", "ha"])
def test_gitleaks_install_precedes_pytest(job_name):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"][job_name]["steps"]
    install = next(
        i for i, step in enumerate(steps) if step.get("name") == "Install Gitleaks"
    )
    pytest_step = next(
        i for i, step in enumerate(steps) if "pytest" in step.get("run", "")
    )
    assert install < pytest_step


@pytest.mark.parametrize("job_name", ["unit", "ha"])
def test_node_install_precedes_ui_test(job_name):
    from tools.check_ci import node_install

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"][job_name]["steps"]
    install = next(
        i for i, step in enumerate(steps) if step.get("name") == "Install Node"
    )
    run = steps[install]["run"]
    assert run == node_install()
    assert install < next(
        i for i, step in enumerate(steps) if "pytest" in step.get("run", "")
    )


def test_secrets_job_uses_checked_gitleaks_and_public_tree():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["secrets"]["steps"]
    install = next(
        step["run"] for step in steps if step.get("name") == "Install Gitleaks"
    )
    assert "v8.30.0/gitleaks_8.30.0_linux_x64.tar.gz" in install
    assert "79a3ab579b53f71efd634f3aaf7e04a0fa0cf206b7ed434638d1547a2470a66e" in install
    assert "sha256sum -c -" in install
    assert any(step.get("with", {}).get("fetch-depth") == 0 for step in steps)
    public = next(
        step["run"] for step in steps if step.get("name") == "Check public tree"
    )
    assert "tools/check_public_tree.py --history --root ." in public
    assert "PRIVATE_DENYLIST" in public
    assert "||" not in public
    assert "continue-on-error" not in workflow["jobs"]["secrets"]


@pytest.mark.parametrize(
    "mutation", ["condition", "needs", "skip", "matrix", "ha_requirements", "compiler"]
)
def test_ci_rejects_weakened_completion(tmp_path, mutation):
    from tools.check_ci import check_jobs

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    complete = workflow["jobs"]["ci-complete"]
    if mutation == "condition":
        del complete["if"]
    elif mutation == "needs":
        complete["needs"].remove("ha")
    elif mutation == "skip":
        complete["steps"][-1]["run"] += " || true"
    elif mutation == "matrix":
        workflow["jobs"]["ha"]["strategy"]["matrix"]["include"].pop()
    elif mutation == "ha_requirements":
        step = next(
            s
            for s in workflow["jobs"]["ha"]["steps"]
            if s.get("name") == "Test all contracts on the exact HA version"
        )
        step["run"] = step["run"].replace('--with "mutagen==1.48.1"', "")
    else:
        steps = workflow["jobs"]["ha"]["steps"]
        workflow["jobs"]["ha"]["steps"] = [
            s for s in steps if s.get("name") != "Check source build compilers"
        ]
    path = tmp_path / "ci.yml"
    path.write_text(yaml.safe_dump(workflow))
    with pytest.raises(ValueError):
        check_jobs(path)


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", "success"])
def test_completion_requires_each_success(result):
    from tools.check_ci import check_jobs, results_pass

    jobs = check_jobs(ROOT / ".github/workflows/ci.yml") - {"ci-complete"}
    needs: dict[str, dict[str, object]] = {job: {"result": "success"} for job in jobs}
    outputs = {"minimum": "success", "current": "success"}
    needs["ha"]["outputs"] = outputs
    assert results_pass(needs)
    for job in jobs:
        changed = {**needs, job: {**needs[job], "result": result}}
        assert results_pass(changed) is (result == "success")
    for member in ("minimum", "current"):
        changed = {
            **needs,
            "ha": {
                "result": "success",
                "outputs": {
                    **outputs,
                    member: result,
                },
            },
        }
        assert results_pass(changed) is (result == "success")
    assert not results_pass({})
    assert not results_pass({key: value for key, value in needs.items() if key != "ha"})


def test_packaging_rejects_bad_metadata(tmp_path):
    import json
    import shutil

    from tools.check_packaging import check_packaging

    shutil.copytree(ROOT / "custom_components", tmp_path / "custom_components")
    shutil.copy(ROOT / "hacs.json", tmp_path / "hacs.json")
    shutil.copy(ROOT / "uv.lock", tmp_path / "uv.lock")
    assert check_packaging(tmp_path) == []
    manifest = tmp_path / "custom_components/assist_live/manifest.json"
    data = json.loads(manifest.read_text())
    data["version"] = "not-a-version"
    data["config_flow"] = False
    manifest.write_text(json.dumps(data))
    (tmp_path / "hacs.json").write_text('{"name": "", "homeassistant": "invalid"}')
    failures = check_packaging(tmp_path)
    assert "manifest version" in failures
    assert "manifest config_flow" in failures
    assert "HACS name" in failures
    assert "HACS homeassistant" in failures


def test_packaging_reports_invalid_inputs(tmp_path):
    from tools.check_packaging import check_packaging

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
def test_package_requires_locked_client(tmp_path, requirements):
    import json
    import shutil

    from tools.check_packaging import check_packaging

    shutil.copytree(ROOT / "custom_components", tmp_path / "custom_components")
    for name in ("hacs.json", "uv.lock"):
        shutil.copy(ROOT / name, tmp_path / name)
    path = tmp_path / "custom_components/assist_live/manifest.json"
    manifest = json.loads(path.read_text())
    manifest["requirements"] = requirements
    path.write_text(json.dumps(manifest))
    assert "manifest requirements" in check_packaging(tmp_path)


def test_client_requirement_tracks_lock_version(tmp_path):
    from tools.check_packaging import valid_client_requirement

    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="assist-live-client"\nversion="1.2.3"\n'
    )
    assert valid_client_requirement(tmp_path, ["assist-live-client==1.2.3"])
    assert not valid_client_requirement(tmp_path, ["assist-live-client==0.0.0"])


def test_ha_plugin_is_active(request):
    assert request.config.pluginmanager.hasplugin("homeassistant")


@pytest.mark.parametrize("name", ["release-please", "release-artifacts"])
def test_release_workflows_fail_closed(name):
    import os
    import subprocess

    workflow = yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())
    assert workflow.get("on", workflow.get(True)) == {"workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read"}
    for job in workflow["jobs"].values():
        guard = job["steps"][0]
        assert guard["env"] == {"APPROVED": "${{ vars.ASSIST_LIVE_RELEASES_APPROVED }}"}
        for value in ("", "false", "true"):
            result = subprocess.run(
                ["bash", "-e", "-c", guard["run"]],
                env={**os.environ, "APPROVED": value},
                capture_output=True,
                check=False,
            )
            assert (result.returncode == 0) is (value == "true")
