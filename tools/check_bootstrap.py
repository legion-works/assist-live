"""Check the experimental integration boundary without Home Assistant."""

import json
import re
import tomllib
from pathlib import Path

if __package__:
    from .check_public_tree import scan_data
else:
    from check_public_tree import scan_data


ROOT_DOCUMENTS = (
    "README.md",
    "ARCHITECTURE.md",
    "STRUCTURE.md",
    "CHANGELOG.md",
    "AGENTS.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
)
REQUIRED_FILES = (
    *ROOT_DOCUMENTS,
    "LICENSE",
    ".gitignore",
    ".editorconfig",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "hacs.json",
    "tests/test_package.py",
    "tests/test_public_tree.py",
    "tools/check_bootstrap.py",
    "tools/check_public_tree.py",
    "docs/design.md",
    "docs/research/contracts.md",
    "docs/verification.md",
    "custom_components/assist_live/__init__.py",
    "custom_components/assist_live/config_flow.py",
    "custom_components/assist_live/manifest.json",
    "custom_components/assist_live/strings.json",
    "custom_components/assist_live/translations/en.json",
)
MANIFEST_FIELDS = {
    "domain": "assist_live",
    "name": "assist-live",
    "codeowners": [],
    "config_flow": True,
    "documentation": "https://github.com/legion-works/assist-live",
    "issue_tracker": "https://github.com/legion-works/assist-live/issues",
    "iot_class": "local_push",
    "integration_type": "service",
    "version": "0.0.0",
}


def check_bootstrap(root):
    root = Path(root)
    failures = []
    for name in REQUIRED_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink() or not path.stat().st_size:
            failures.append(f"required file {name}")
    if failures:
        return failures
    try:
        license_text = (root / "LICENSE").read_text(encoding="utf-8")
        if not all(
            part in license_text
            for part in (
                "Apache License",
                "Version 2.0, January 2004",
                "END OF TERMS AND CONDITIONS",
            )
        ):
            failures.append("Apache-2.0 license")
        package = root / "custom_components/assist_live"
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        if list(manifest) != [
            "domain",
            "name",
            *sorted(key for key in manifest if key not in {"domain", "name"}),
        ]:
            failures.append("manifest key order")
        for field, expected in MANIFEST_FIELDS.items():
            value = manifest.get(field)
            if type(value) is not type(expected) or value != expected:
                failures.append(f"manifest {field}")
        requirements = manifest.get("requirements")
        if not (
            requirements == []
            or (
                isinstance(requirements, list)
                and len(requirements) == 1
                and isinstance(requirements[0], str)
                and re.fullmatch(r"assist-live-client==\d+\.\d+\.\d+", requirements[0])
            )
        ):
            failures.append("manifest requirements")
        if manifest.get("dependencies", []) != []:
            failures.append("bootstrap dependencies")
        strings = json.loads((package / "strings.json").read_text(encoding="utf-8"))
        english = json.loads(
            (package / "translations/en.json").read_text(encoding="utf-8")
        )
        if strings != english or not strings["config"]["abort"]["bootstrap_only"]:
            failures.append("config flow strings")
        for translation in (strings, english):
            steps = translation["config"].get("step")
            user_step = steps.get("user") if isinstance(steps, dict) else None
            if not isinstance(user_step, dict) or any(
                not isinstance(user_step.get(field), str)
                or not user_step[field].strip()
                for field in ("title", "description")
            ):
                failures.append("config flow steps")
                break
        hacs = json.loads((root / "hacs.json").read_text(encoding="utf-8"))
        if hacs != {"name": "assist-live", "homeassistant": "2026.10.1"}:
            failures.append("HACS metadata")
        metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        if metadata["project"]["requires-python"] != ">=3.14.2":
            failures.append("Python requirement")
        if (root / ".python-version").read_text().strip() != "3.14.2":
            failures.append("Python version")
        if (root / "ha-app").exists():
            failures.append("HA app outside integration repository")
        for name in REQUIRED_FILES:
            if scan_data((root / name).read_bytes(), (), set(), name):
                failures.append(f"public data {name}")
    except OSError, ValueError, KeyError, TypeError:
        failures.append("package metadata input")
    return failures


def main():
    failures = check_bootstrap(Path(__file__).resolve().parents[1])
    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print("PASS integration bootstrap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
