from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.parametrize("mutation", ["condition", "needs", "skip", "matrix"])
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
    else:
        workflow["jobs"]["ha"]["strategy"]["matrix"]["include"].pop()
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
