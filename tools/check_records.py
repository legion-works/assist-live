import argparse
import difflib
import json
import os
import re
import subprocess
from pathlib import Path

RECORD = "docs/verification.md"


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True
    ).stdout


def comparison_base(root: Path) -> str:
    if os.environ.get("GITHUB_EVENT_NAME") == "push":
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        before = event["before"]
        if not isinstance(before, str) or not re.fullmatch(r"[0-9a-f]{40,64}", before):
            raise ValueError("invalid previous main commit")
        if not before.strip("0"):
            raise ValueError("previous main commit unavailable")
        return before
    main = "origin/main" if os.environ.get("GITHUB_EVENT_NAME") else "main"
    return git(root, "merge-base", main, "HEAD").decode().strip()


def violations(before: bytes, after: bytes) -> list[str]:
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    if new[: len(old)] == old:
        return []
    failures = []
    for tag, start, end, new_start, new_end in difflib.SequenceMatcher(
        None, old, new, autojunk=False
    ).get_opcodes():
        if tag == "equal" or (tag == "insert" and start == len(old)):
            continue
        first, last = (new_start, new_end) if tag == "insert" else (start, end)
        for line in range(first + 1, last + 1):
            failures.append(f"{RECORD}:{line}: {tag} before end of record")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--base")
    args = parser.parse_args(argv)
    try:
        base = args.base or comparison_base(args.root)
        before = git(args.root, "show", f"{base}:{RECORD}")
        try:
            after = (args.root / RECORD).read_bytes()
        except FileNotFoundError:
            after = b""
        failures = violations(before, after)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"FAIL {RECORD}: comparison unavailable ({type(error).__name__})")
        return 1
    if failures:
        print("\n".join(f"FAIL {failure}" for failure in failures))
        return 1
    print(f"PASS {RECORD}: append-only relative to {base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
