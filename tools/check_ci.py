"""Check mandatory CI jobs and reject incomplete results."""

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
RESULT_COMMAND = 'uv run --locked python tools/check_ci.py --results "$NEEDS_JSON"'
HA_MATRIX = [
    {"ha": "2026.10.1", "plugin": "0.13.372", "member": "minimum"},
    {"ha": "2026.10.3", "plugin": "0.13.374", "member": "current"},
]


def check_jobs(path: str | Path) -> set[str]:
    workflow = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    failures = []
    if set(jobs) != REQUIRED_JOBS:
        failures.append("required jobs")
    complete = jobs.get("ci-complete", {})
    if set(complete.get("needs", [])) != REQUIRED_JOBS - {"ci-complete"}:
        failures.append("completion dependencies")
    if complete.get("if") != "${{ always() }}":
        failures.append("completion condition")
    steps = complete.get("steps", [])
    if not steps or steps[-1].get("run") != RESULT_COMMAND:
        failures.append("completion command")
    elif steps[-1].get("env") != {"NEEDS_JSON": "${{ toJSON(needs) }}"}:
        failures.append("completion inputs")
    for name, job in jobs.items():
        if job.get("continue-on-error") or (name != "ci-complete" and "if" in job):
            failures.append(f"job result {name}")
        if any(step.get("continue-on-error") or "if" in step for step in job["steps"]):
            failures.append(f"step result {name}")
    ha = jobs.get("ha", {})
    if ha.get("strategy") != {"fail-fast": False, "matrix": {"include": HA_MATRIX}}:
        failures.append("HA matrix")
    if ha.get("outputs") != {
        "minimum": "${{ steps.result.outputs.minimum }}",
        "current": "${{ steps.result.outputs.current }}",
    }:
        failures.append("HA member outputs")
    if failures:
        raise ValueError(", ".join(failures))
    return set(jobs)


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
        outputs.get(member) == "success" for member in ("minimum", "current")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflow",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml",
    )
    parser.add_argument("--results")
    args = parser.parse_args(argv)
    try:
        check_jobs(args.workflow)
        if args.results is not None and not results_pass(json.loads(args.results)):
            raise ValueError("incomplete CI results")
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        print(f"FAIL CI {error}")
        return 1
    print("PASS CI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
