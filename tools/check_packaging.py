"""Check local package fields, not Marketplace or HACS-default acceptance."""

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from packaging.version import InvalidVersion, Version

if TYPE_CHECKING or __package__:
    from .check_public_tree import git
else:
    from check_public_tree import git

PACKAGE_FILES = {
    "__init__.py",
    "config_flow.py",
    "const.py",
    "ledger.py",
    "manifest.json",
    "repairs.py",
    "strings.json",
    "stt.py",
    "translations/en.json",
    "tts.py",
}


def valid_client_requirement(root: Path, requirements: object) -> bool:
    try:
        locked = tomllib.loads((root / "uv.lock").read_text())
        clients = [p for p in locked["package"] if p["name"] == "assist-live-client"]
        return len(clients) == 1 and requirements == [
            f"assist-live-client=={clients[0]['version']}"
        ]
    except OSError, ValueError, KeyError, TypeError:
        return False


def check_packaging(root: str | Path) -> list[str]:
    root = Path(root)
    failures = []
    package = root / "custom_components/assist_live"
    try:
        if (root / ".git").exists():
            paths = [
                Path(os.fsdecode(name)).relative_to("custom_components/assist_live")
                for name in git(
                    root,
                    "ls-files",
                    "-co",
                    "--exclude-standard",
                    "-z",
                    "--",
                    "custom_components/assist_live",
                ).split(b"\0")
                if name
            ]
        else:
            paths = [
                path.relative_to(package)
                for path in package.rglob("*")
                if path.is_file()
            ]
        for path in sorted(paths):
            name = path.as_posix()
            if "assist_live_test_agent" in name.casefold():
                failures.append(f"test agent package path {name}")
            if name not in PACKAGE_FILES:
                failures.append(f"unexpected package file {name}")
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        hacs = json.loads((root / "hacs.json").read_text(encoding="utf-8"))
        for field, expected in {
            "domain": "assist_live",
            "name": "assist-live",
            "config_flow": True,
            "integration_type": "service",
            "iot_class": "local_push",
        }.items():
            if (
                type(manifest.get(field)) is not type(expected)
                or manifest.get(field) != expected
            ):
                failures.append(f"manifest {field}")
        if list(manifest) != [
            "domain",
            "name",
            *sorted(set(manifest) - {"domain", "name"}),
        ]:
            failures.append("manifest key order")
        owners = manifest.get("codeowners")
        if not isinstance(owners, list) or any(
            not isinstance(owner, str) or not re.fullmatch(r"@[\w-]+", owner)
            for owner in owners
        ):
            failures.append("manifest codeowners")
        for field in ("documentation", "issue_tracker"):
            value = manifest.get(field)
            if not isinstance(value, str) or not value.startswith(
                "https://github.com/legion-works/assist-live"
            ):
                failures.append(f"manifest {field}")
        try:
            Version(manifest.get("version", ""))
        except InvalidVersion, TypeError:
            failures.append("manifest version")
        requirements = manifest.get("requirements")
        if not valid_client_requirement(root, requirements):
            failures.append("manifest requirements")
        if hacs.get("name") != "assist-live":
            failures.append("HACS name")
        if hacs.get("homeassistant") != "2026.10.1":
            failures.append("HACS homeassistant")
        if set(hacs) - {"name", "homeassistant"}:
            failures.append("HACS fields")
        for name in (
            "__init__.py",
            "config_flow.py",
            "strings.json",
            "translations/en.json",
        ):
            if not (package / name).is_file():
                failures.append(f"package file {name}")
        strings = json.loads((package / "strings.json").read_text(encoding="utf-8"))
        english = json.loads(
            (package / "translations/en.json").read_text(encoding="utf-8")
        )
        if strings != english or not strings["config"]["step"]["user"]:
            failures.append("config flow strings")
    except subprocess.CalledProcessError:
        failures.append("package file inventory")
    except OSError, ValueError, KeyError, TypeError, AttributeError:
        failures.append("package metadata input")
    return failures


def main() -> int:
    failures = check_packaging(Path(__file__).resolve().parents[1])
    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print("PASS packaging (local check, not Marketplace or HACS-default acceptance)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
