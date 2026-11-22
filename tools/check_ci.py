"""Check mandatory CI jobs and reject incomplete results."""

import argparse
import json
import re
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
RECORDS_COMMAND = "uv run --locked python tools/check_records.py"
RELEASE_PIN_COMMAND = (
    "uv run --locked python tools/check_release_pin.py"
    " --manifest custom_components/assist_live/manifest.json"
    ' --pypi-version "$CLIENT_VERSION" --require-published'
)
HA_MATRIX = [
    {"ha": "2026.10.1", "plugin": "0.13.372", "member": "minimum"},
    {"ha": "2026.10.4", "plugin": "0.13.375", "member": "current"},
    {"ha": "2026.11.2", "plugin": "0.13.375", "member": "compatibility"},
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


def ffmpeg_install() -> str:
    return (
        "sudo apt-get update\n"
        "sudo apt-get install --yes --no-install-recommends ffmpeg\n"
        "ffmpeg -version | head -1\n"
    )


def ha_2026_11_commands(client_requirement: str) -> list[list[str]]:
    python = ".venv-ha-2026-11/bin/python"
    requirements = [
        "home-assistant-intents==2026.10.27"
        if requirement.startswith("home-assistant-intents==")
        else requirement
        for requirement in HA_COMPONENT_REQUIREMENTS
    ]
    return [
        ["uv", "venv", "--allow-existing", "--python", "3.14.2", ".venv-ha-2026-11"],
        [
            "uv",
            "pip",
            "install",
            "--python",
            python,
            "--override",
            "tests/ha-2026-11-override.txt",
            "homeassistant==2026.11.2",
            "pytest-homeassistant-custom-component==0.13.375",
            "PyYAML==6.0.3",
            *requirements,
            client_requirement,
        ],
        [python, "-m", "pytest", "-p", "tools.ha_2026_11_pytest", "tests/", "-q"],
    ]


def check_jobs(path: str | Path) -> set[str]:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads(
        (root / "custom_components/assist_live/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    requirements = manifest["requirements"]
    if (
        not isinstance(requirements, list)
        or len(requirements) != 1
        or not isinstance(requirements[0], str)
        or not re.fullmatch(
            r"assist-live-client==[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?",
            requirements[0],
        )
    ):
        raise ValueError("client pin manifest.json")
    client_requirement = requirements[0]
    client_version = client_requirement.split("==")[1]
    workflow = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    failures = []
    if [
        requirement
        for requirement in project["dependency-groups"]["dev"]
        if requirement.startswith("assist-live-client")
    ] != [client_requirement] or "assist-live-client" in project.get("tool", {}).get(
        "uv", {}
    ).get("sources", {}):
        failures.append("client pin pyproject.toml")
    release = yaml.safe_load(
        (root / ".github/workflows/release-artifacts.yml").read_text(encoding="utf-8")
    )
    if [
        (step.get("env", {}).get("CLIENT_VERSION"), shlex.split(step.get("run", "")))
        for job in release["jobs"].values()
        for step in job["steps"]
        if "tools/check_release_pin.py" in step.get("run", "")
    ] != [(client_version, shlex.split(RELEASE_PIN_COMMAND))]:
        failures.append("client pin .github/workflows/release-artifacts.yml")
    current = next(member for member in HA_MATRIX if member["member"] == "current")
    plugin_pins = [
        requirement
        for requirement in project["dependency-groups"]["dev"]
        if requirement.startswith("pytest-homeassistant-custom-component")
    ]
    if plugin_pins != [f"pytest-homeassistant-custom-component=={current['plugin']}"]:
        failures.append("current plugin pyproject.toml")
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
    lint_steps = jobs.get("lint", {}).get("steps", [])
    if sum(step.get("run") == RECORDS_COMMAND for step in lint_steps) != 1:
        failures.append("records guard lint command")
    checkout_depths = [
        step.get("with", {}).get("fetch-depth")
        for step in lint_steps
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    if (
        len(checkout_depths) != 1
        or type(checkout_depths[0]) is not int
        or checkout_depths[0] != 0
    ):
        failures.append("records guard lint checkout")
    ha = jobs.get("ha", {})
    for name in ("unit", "ha"):
        installs = [
            step.get("run")
            for step in jobs.get(name, {}).get("steps", [])
            if step.get("name") == "Install Node"
        ]
        if installs != [node_install()]:
            failures.append(f"Node install {name}")
        steps = jobs.get(name, {}).get("steps", [])
        ffmpeg_steps = [
            (index, step.get("run"))
            for index, step in enumerate(steps)
            if step.get("name") == "Install FFmpeg"
        ]
        first_pytest = next(
            (
                index
                for index, step in enumerate(steps)
                if "pytest" in step.get("run", "")
            ),
            -1,
        )
        if (
            len(ffmpeg_steps) != 1
            or ffmpeg_steps[0][1] != ffmpeg_install()
            or not 0 <= ffmpeg_steps[0][0] < first_pytest
        ):
            failures.append(f"FFmpeg install {name}")
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
                or step.get("name") == "Test all contracts on the exact HA version"
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
    compatibility_commands = ha_2026_11_commands(client_requirement)
    expected_lines = [
        shlex.split('if [ "$HA_VERSION" = "2026.11.2" ]; then'),
        *compatibility_commands,
        ["else"],
        required,
        ["fi"],
    ]
    actual_lines = [shlex.split(line) for line in command.splitlines() if line.strip()]
    if actual_lines != expected_lines:
        failures.append("HA component requirements command")
    if [
        part for part in shlex.split(command) if part.startswith("assist-live-client")
    ] != [client_requirement, client_requirement]:
        failures.append("client pin .github/workflows/ci.yml")
    taskfile = yaml.safe_load((root / "Taskfile.yml").read_text(encoding="utf-8"))
    if [
        shlex.split(cmd)
        for cmd in taskfile["tasks"].get("ha-2026-11", {}).get("cmds", [])
    ] != compatibility_commands:
        failures.append("HA 2026.11.2 Taskfile.yml")
    if (
        root / "tests/ha-2026-11-override.txt"
    ).read_text() != "homeassistant==2026.11.2\n":
        failures.append("HA 2026.11.2 override")
    current_task = taskfile["tasks"]["check"]
    if not any(
        isinstance(command, dict) and command.get("task") == "records-check"
        for command in current_task.get("cmds", [])
    ):
        failures.append("records guard Taskfile.yml")
    description = current_task.get("desc", "")
    version_pattern = r"\b(?:HA\s+|homeassistant==)(\d{4}\.\d+\.\d+)\b"
    description_versions = re.findall(version_pattern, description)
    command_versions = [
        version
        for cmd in current_task.get("cmds", [])
        if isinstance(cmd, str)
        for version in re.findall(version_pattern, cmd)
    ]
    if not description_versions or any(
        version != current["ha"]
        for version in [*description_versions, *command_versions]
    ):
        failures.append("current HA Taskfile.yml")
    task_pins = [
        part
        for task in taskfile["tasks"].values()
        for cmd in task.get("cmds", [])
        if isinstance(cmd, str)
        for part in shlex.split(cmd)
        if part.startswith("assist-live-client")
    ]
    if task_pins != [client_requirement, client_requirement]:
        failures.append("client pin Taskfile.yml")
    locked = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    resolved_pins = [
        (package["version"], package["source"])
        for package in locked["package"]
        if package["name"] == "assist-live-client"
    ]
    declared_pins = [
        dependency
        for package in locked["package"]
        for group in package.get("metadata", {}).get("requires-dev", {}).values()
        for dependency in group
        if dependency["name"] == "assist-live-client"
    ]
    if resolved_pins != [
        (client_version, {"registry": "https://pypi.org/simple"})
    ] or declared_pins != [
        {"name": "assist-live-client", "specifier": f"=={client_version}"}
    ]:
        failures.append("client pin uv.lock")
    if ha.get("strategy") != {"fail-fast": False, "matrix": {"include": HA_MATRIX}}:
        failures.append("HA matrix")
    if ha.get("outputs") != {
        "minimum": "${{ steps.result.outputs.minimum }}",
        "current": "${{ steps.result.outputs.current }}",
        "compatibility": "${{ steps.result.outputs.compatibility }}",
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
        outputs.get(member["member"]) == "success" for member in HA_MATRIX
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
