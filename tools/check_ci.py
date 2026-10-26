"""Check mandatory CI jobs and reject incomplete results."""

import argparse
import json
import shlex
import tomllib
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
HA_COMPONENT_REQUIREMENTS = [
    "gazetteer-matcher==1.1.0",
    "ha-ffmpeg==3.2.2",
    "hassil==3.12.1",
    "home-assistant-intents==2026.10.6",
    "mutagen==1.48.1",
    "pymicro-vad==1.0.1",
    "pyspeex-noise==1.0.2",
]
NODE_VERSION = "22.22.0"
NODE_SHA256 = "9aa8e9d2298ab68c600bd6fb86a6c13bce11a4eca1ba9b39d79fa021755d7c37"


def node_install() -> str:
    artifact = f"node-v{NODE_VERSION}-linux-x64"
    return (
        f"curl -fsSL https://nodejs.org/dist/v{NODE_VERSION}/{artifact}.tar.xz"
        ' -o "$RUNNER_TEMP/node.tar.xz"\n'
        f'echo "{NODE_SHA256}  $RUNNER_TEMP/node.tar.xz" | sha256sum -c -\n'
        'tar -xJf "$RUNNER_TEMP/node.tar.xz" -C "$RUNNER_TEMP"\n'
        f'echo "$RUNNER_TEMP/node-v{NODE_VERSION}-linux-x64/bin" >> "$GITHUB_PATH"\n'
    )


def check_jobs(path: str | Path) -> set[str]:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    source = project["tool"]["uv"]["sources"]["assist-live-client"]
    repository, revision, subdirectory = (
        source["git"],
        source["rev"],
        source["subdirectory"],
    )
    client_requirement = (
        f"assist-live-client @ git+{repository}@{revision}#subdirectory={subdirectory}"
    )
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
    for name in ("unit", "ha"):
        installs = [
            step.get("run")
            for step in jobs.get(name, {}).get("steps", [])
            if step.get("name") == "Install Node"
        ]
        if installs != [node_install()]:
            failures.append(f"Node install {name}")
    for name in ("lint", "typecheck", "unit", "ha"):
        job_steps = jobs.get(name, {}).get("steps", [])
        compilers = [
            i
            for i, step in enumerate(job_steps)
            if step.get("name") == "Check source build compilers"
            and step.get("run") == "gcc --version && g++ --version"
        ]
        first_uv = next(
            (
                i
                for i, step in enumerate(job_steps)
                if step.get("run", "").startswith("uv ")
            ),
            -1,
        )
        if len(compilers) != 1 or not 0 <= compilers[0] < first_uv:
            failures.append(f"source build compilers {name}")
    command = next(
        (
            step.get("run", "")
            for step in ha.get("steps", [])
            if step.get("name") == "Test all contracts on the exact HA version"
        ),
        "",
    )
    required = [
        "uv",
        "run",
        "--no-project",
        "--python",
        "3.14.2",
        "--with",
        "homeassistant==$HA_VERSION",
        "--with",
        "pytest-homeassistant-custom-component==$PLUGIN_VERSION",
        "--with",
        "PyYAML==6.0.3",
    ]
    for requirement in [*HA_COMPONENT_REQUIREMENTS, client_requirement]:
        required.extend(["--with", requirement])
    required.extend(["pytest", "tests/", "-q"])
    if shlex.split(command) != required:
        failures.append("HA component requirements command")
    if [
        part for part in shlex.split(command) if part.startswith("assist-live-client")
    ] != [client_requirement]:
        failures.append("client pin .github/workflows/ci.yml")
    taskfile = yaml.safe_load((root / "Taskfile.yml").read_text(encoding="utf-8"))
    task_pins = [
        part
        for task in taskfile["tasks"].values()
        for cmd in task.get("cmds", [])
        if isinstance(cmd, str)
        for part in shlex.split(cmd)
        if part.startswith("assist-live-client")
    ]
    if task_pins != [client_requirement]:
        failures.append("client pin Taskfile.yml")
    locked = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    lock_url = f"{repository}?subdirectory={subdirectory}&rev={revision}"
    resolved_pins = [
        package["source"].get("git")
        for package in locked["package"]
        if package["name"] == "assist-live-client"
    ]
    declared_pins = [
        dependency.get("git")
        for package in locked["package"]
        for group in package.get("metadata", {}).get("requires-dev", {}).values()
        for dependency in group
        if dependency["name"] == "assist-live-client"
    ]
    if resolved_pins != [f"{lock_url}#{revision}"] or declared_pins != [lock_url]:
        failures.append("client pin uv.lock")
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
