"""Check the release publisher and reject incomplete CI results."""

import argparse
import json
from pathlib import Path

import yaml

REQUIRED_JOBS = {
    "lint",
    "typecheck",
    "unit",
    "ha",
    "hassfest",
    "hacs",
    "secrets",
    "ci-complete",
}
RELEASE_PIN_COMMAND = (
    "uv run --locked python tools/check_release_pin.py"
    " --manifest custom_components/assist_live/manifest.json"
    ' --pypi-version "$CLIENT_VERSION" --require-published'
)
RELEASE_DEFAULT_BRANCH = (
    "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
)
HA_MATRIX = [
    {"ha": "2026.10.1", "plugin": "0.13.372", "member": "minimum"},
    {"ha": "2026.10.4", "plugin": "0.13.375", "member": "current"},
    {"ha": "2026.11.3", "plugin": "0.13.375", "member": "compatibility"},
]


def results_pass(needs: object) -> bool:
    if not isinstance(needs, dict) or set(needs) != REQUIRED_JOBS - {"ci-complete"}:
        return False
    if any(
        not isinstance(job, dict) or job.get("result") != "success"
        for job in needs.values()
    ):
        return False
    outputs = needs["ha"].get("outputs", {})
    return isinstance(outputs, dict) and all(
        outputs.get(member["member"]) == "success" for member in HA_MATRIX
    )


def check_release_publisher(root: Path) -> None:
    workflows = root / ".github/workflows"
    publisher = yaml.safe_load(
        (workflows / "release-please.yml").read_text(encoding="utf-8")
    )
    archive = yaml.safe_load(
        (workflows / "release-artifacts.yml").read_text(encoding="utf-8")
    )
    release = publisher["jobs"]["release-please"]
    call = publisher["jobs"].get("archive", {})
    job = archive["jobs"]["archive"]
    pins = [step for step in job["steps"] if step.get("run") == RELEASE_PIN_COMMAND]
    if (
        release.get("needs") != "archive"
        or "if" in release
        or call
        != {
            "if": RELEASE_DEFAULT_BRANCH,
            "uses": "./.github/workflows/release-artifacts.yml",
        }
        or "workflow_call" not in archive.get("on", archive.get(True, {}))
        or "if" in job
        or "continue-on-error" in job
        or len(pins) != 1
        or "if" in pins[0]
        or "continue-on-error" in pins[0]
    ):
        raise ValueError("release publisher dependency")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results")
    args = parser.parse_args(argv)
    try:
        check_release_publisher(Path(__file__).resolve().parents[1])
        if args.results is not None and not results_pass(json.loads(args.results)):
            raise ValueError("incomplete CI results")
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        print(f"FAIL CI {error}")
        return 1
    print("PASS CI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
