"""Check the exact client version before an integration release."""

import argparse
import json
import re
from importlib.metadata import version
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def published(version: str) -> bool:
    try:
        with urlopen(
            f"https://pypi.org/pypi/assist-live-client/{version}/json", timeout=10
        ) as response:
            result = json.load(response)
        return result["info"]["version"] == version and bool(result["urls"])
    except URLError, OSError, ValueError, KeyError, TypeError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--pypi-version", required=True)
    parser.add_argument("--require-published", action="store_true")
    args = parser.parse_args(argv)
    try:
        requirements = json.loads(args.manifest.read_text())["requirements"]
        if not re.fullmatch(
            r"[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?", args.pypi_version
        ):
            raise ValueError("invalid client version")
        pins = [item for item in requirements if item.startswith("assist-live-client")]
        if pins != [f"assist-live-client=={args.pypi_version}"]:
            raise ValueError("manifest client pin differs")
        if version("assist-live-client") != args.pypi_version:
            raise ValueError("pinned client package version differs")
        if args.require_published and not published(args.pypi_version):
            raise ValueError("exact client version is not published on PyPI")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"FAIL release pin: {error}")
        return 1
    print("PASS release pin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
