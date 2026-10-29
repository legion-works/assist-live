import shlex
from pathlib import Path

import pytest
import yaml
from test_public_tree import denylist, repository  # noqa: F401

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


@pytest.mark.parametrize("mode", ["unset", "valid", "missing", "hit"])
def test_task_privacy_branch_keeps_other_checks(repository, denylist, tmp_path, mode):  # noqa: F811
    import os
    import shutil
    import subprocess
    import sys

    tool = repository / "tools/check_public_tree.py"
    tool.parent.mkdir()
    shutil.copy(ROOT / "tools/check_public_tree.py", tool)
    commands = yaml.safe_load((ROOT / "Taskfile.yml").read_text())["tasks"]["check"][
        "cmds"
    ]
    branch = commands[-1]
    assert isinstance(branch, str)
    assert all(
        command in commands
        for command in (
            {"task": "lint"},
            {"task": "typecheck"},
            {"task": "unit"},
            "uv run --locked python tools/check_bootstrap.py",
            "uv run --locked python tools/check_ci.py",
            "gitleaks dir . --redact --exit-code 1",
            "gitleaks git . --redact --exit-code 1",
        )
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


def test_ci_current_member_versions():
    from tools.check_ci import HA_MATRIX

    assert HA_MATRIX == [
        {"ha": "2026.10.1", "plugin": "0.13.372", "member": "minimum"},
        {"ha": "2026.10.4", "plugin": "0.13.375", "member": "current"},
    ]


def test_ci_rejects_previous_current_member(tmp_path):
    from tools.check_ci import main

    path = tmp_path / "ci.yml"
    source = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "2026.10.4" in source
    path.write_text(source.replace("2026.10.4", "2026.10.3"))
    assert main(["--workflow", str(path)]) == 1


@pytest.mark.parametrize("carrier", ["plugin", "task_description", "task_command"])
def test_ci_rejects_stale_current_carriers(tmp_path, monkeypatch, carrier):
    import shutil

    from tools.check_ci import check_jobs

    for name in (
        "pyproject.toml",
        "uv.lock",
        "Taskfile.yml",
        ".github/workflows/ci.yml",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, target)
    monkeypatch.setattr("tools.check_ci.__file__", str(tmp_path / "tools/check_ci.py"))
    assert check_jobs(tmp_path / ".github/workflows/ci.yml")
    if carrier == "plugin":
        target = tmp_path / "pyproject.toml"
        target.write_text(target.read_text().replace("0.13.375", "0.13.374"))
        failure = "current plugin pyproject.toml"
    else:
        target = tmp_path / "Taskfile.yml"
        taskfile = yaml.safe_load(target.read_text())
        task = taskfile["tasks"]["check"]
        if carrier == "task_description":
            task["desc"] = task["desc"].replace("2026.10.4", "2026.10.3")
        else:
            task["cmds"].append("uv run --with homeassistant==2026.10.3 pytest")
        target.write_text(yaml.safe_dump(taskfile))
        failure = "current HA Taskfile.yml"
    with pytest.raises(ValueError, match=failure):
        check_jobs(tmp_path / ".github/workflows/ci.yml")


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


@pytest.mark.parametrize(
    "carrier,field",
    [
        ("uv.lock", "all"),
        ("uv.lock", "resolved"),
        ("uv.lock", "declared"),
        ("Taskfile.yml", "all"),
        (".github/workflows/ci.yml", "all"),
    ],
)
def test_client_pin_mismatch_has_named_failure(tmp_path, carrier, field, monkeypatch):
    import shutil
    import tomllib

    from tools.check_ci import check_jobs

    for name in (
        "pyproject.toml",
        "uv.lock",
        "Taskfile.yml",
        ".github/workflows/ci.yml",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, target)
    revision = tomllib.loads((tmp_path / "pyproject.toml").read_text())["tool"]["uv"][
        "sources"
    ]["assist-live-client"]["rev"]
    target = tmp_path / carrier
    content = target.read_text()
    if field == "resolved":
        content = content.replace(f"#{revision}", f"#{'0' * 40}")
    elif field == "declared":
        content = content.replace(f'rev={revision}"', f'rev={"0" * 40}"')
    else:
        content = content.replace(revision, "0" * 40)
    target.write_text(content)
    monkeypatch.setattr("tools.check_ci.__file__", str(tmp_path / "tools/check_ci.py"))
    with pytest.raises(ValueError, match=f"client pin {carrier}"):
        check_jobs(tmp_path / ".github/workflows/ci.yml")


def test_client_pin_uses_project_revision(tmp_path, monkeypatch):
    import tomllib

    from tools.check_ci import check_jobs

    revision = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"][
        "sources"
    ]["assist-live-client"]["rev"]
    for name in (
        "pyproject.toml",
        "uv.lock",
        "Taskfile.yml",
        ".github/workflows/ci.yml",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / name).read_text().replace(revision, "1" * 40))
    monkeypatch.setattr("tools.check_ci.__file__", str(tmp_path / "tools/check_ci.py"))
    assert check_jobs(tmp_path / ".github/workflows/ci.yml")


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

    shutil.copytree(
        ROOT / "custom_components",
        tmp_path / "custom_components",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
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


def test_package_file_set_is_explicit():
    from tools.check_packaging import PACKAGE_FILES

    assert PACKAGE_FILES == {
        "__init__.py",
        "config_flow.py",
        "const.py",
        "diagnostics.py",
        "ledger.py",
        "manifest.json",
        "repairs.py",
        "sensor.py",
        "strings.json",
        "stt.py",
        "stt_fallback.py",
        "translations/en.json",
        "tts.py",
        "tts_fallback.py",
    }


@pytest.fixture
def packaged_root(tmp_path):
    import shutil

    shutil.copytree(
        ROOT / "custom_components",
        tmp_path / "custom_components",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ("hacs.json", "uv.lock", ".gitignore"):
        shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


@pytest.mark.parametrize("git_tree", [False, True], ids=["archive", "git"])
@pytest.mark.parametrize(
    "extra",
    ["notes.txt", "nested/conversation.py", "test_agent", "assist_live_test_agent"],
)
def test_package_rejects_unexpected_files_and_fixture_paths(
    packaged_root, extra, git_tree
):
    import shutil
    import subprocess

    from tools.check_packaging import check_packaging

    if git_tree:
        subprocess.run(
            ["git", "-C", str(packaged_root), "init", "-b", "main"],
            check=True,
            capture_output=True,
        )
    assert check_packaging(packaged_root) == []
    destination = packaged_root / "custom_components/assist_live" / extra
    if extra == "test_agent":
        shutil.copytree(
            ROOT / "tests/fixtures/custom_components/assist_live_test_agent",
            destination,
        )
    elif extra == "assist_live_test_agent":
        destination.mkdir()
        (destination / "__init__.py").write_text("")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("test-only")
    failures = check_packaging(packaged_root)
    if extra == "assist_live_test_agent":
        assert f"test agent package path {extra}/__init__.py" in failures
    else:
        assert any(
            failure.startswith("unexpected package file ") for failure in failures
        )


@pytest.mark.parametrize("git_tree", [False, True], ids=["archive", "git"])
def test_package_bytecode_is_excluded_only_by_git(packaged_root, git_tree):
    import subprocess

    from tools.check_packaging import check_packaging

    if git_tree:
        subprocess.run(
            ["git", "-C", str(packaged_root), "init", "-b", "main"],
            check=True,
            capture_output=True,
        )
    package = packaged_root / "custom_components/assist_live"
    for name in ("__pycache__/stt.cpython-314.pyc", "stt.pyc"):
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test-only")
        if git_tree:
            subprocess.run(
                ["git", "-C", str(packaged_root), "check-ignore", str(path)],
                check=True,
                capture_output=True,
            )
    failures = check_packaging(packaged_root)
    if git_tree:
        assert failures == []
    else:
        assert "unexpected package file __pycache__/stt.cpython-314.pyc" in failures
        assert "unexpected package file stt.pyc" in failures


def test_package_refuses_tracked_ignored_bytecode(packaged_root):
    import subprocess

    from tools.check_packaging import check_packaging

    subprocess.run(
        ["git", "-C", str(packaged_root), "init", "-b", "main"],
        check=True,
        capture_output=True,
    )
    path = packaged_root / "custom_components/assist_live/stt.pyc"
    path.write_bytes(b"test-only")
    subprocess.run(
        ["git", "-C", str(packaged_root), "add", "-f", str(path)],
        check=True,
        capture_output=True,
    )
    assert "unexpected package file stt.pyc" in check_packaging(packaged_root)


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

    shutil.copytree(
        ROOT / "custom_components",
        tmp_path / "custom_components",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
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


def test_release_archive_checks_published_client_before_build(tmp_path):
    import os
    import subprocess
    import sys

    steps = yaml.safe_load(
        (ROOT / ".github/workflows/release-artifacts.yml").read_text()
    )["jobs"]["archive"]["steps"]
    checks = [
        (i, step)
        for i, step in enumerate(steps)
        if "tools/check_release_pin.py" in step.get("run", "")
    ]
    assert len(checks) == 1
    index, step = checks[0]
    assert index < next(
        i for i, s in enumerate(steps) if "git archive" in s.get("run", "")
    )
    assert not any("upload-artifact" in s.get("uses", "") for s in steps)
    assert "--require-published" in shlex.split(step["run"])
    uv = tmp_path / "uv"
    uv.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "assert sys.argv[1:4] == ['run', '--locked', 'python']\n"
        "assert '--require-published' in sys.argv\n"
        "assert sys.argv[sys.argv.index('--pypi-version') + 1] == '0.0.0'\n"
        "raise SystemExit(19)\n"
    )
    uv.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={
            **os.environ,
            **step.get("env", {}),
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 19, result.stderr
