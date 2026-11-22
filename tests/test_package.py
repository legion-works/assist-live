def test_package_boundary():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / "custom_components/assist_live/manifest.json").read_text()
    )
    assert manifest["domain"] == "assist_live"
    assert manifest["config_flow"] is True
    assert not (root / "ha-app").exists()
    assert "Apache" in (root / "LICENSE").read_text()


def test_release_archive_passes_its_extracted_packaging_check(tmp_path):
    import os
    import subprocess
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load(
        (root / ".github/workflows/release-artifacts.yml").read_text()
    )
    command = next(
        step["run"]
        for step in workflow["jobs"]["archive"]["steps"]
        if step.get("name") == "Build and check the archive without publication"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", command],
        cwd=root,
        env={**os.environ, "RUNNER_TEMP": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"release_archive_packaging_failed\n{result.stdout}\n{result.stderr}"
    )
