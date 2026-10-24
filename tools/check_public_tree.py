"""Check public data without printing private matches."""

import argparse
import hashlib
import ipaddress
import os
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Collection, Iterable, Iterator, Sequence
from pathlib import Path

IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w)(?!\.\d)")
IPV6 = re.compile(
    r"(?<![\w:])(?:[0-9a-fA-F]{0,4}:){1,7}[0-9a-fA-F:.]+"
    r"(?:%[\w.-]+)?"
)
PRIVATE_PATH = re.compile(
    r"(?<![A-Za-z0-9._-])/(?:ho[m]e|U[s]ers)/[^/\s]+|[a-z]:\\U[s]ers\\[^\\\s]+",
    re.IGNORECASE,
)
PRIVATE_V4 = tuple(
    ipaddress.IPv4Network((address, prefix))
    for address, prefix in (
        (0x0A000000, 8),
        (0xAC100000, 12),
        (0xC0A80000, 16),
        (0xA9FE0000, 16),
        (0x64400000, 10),
    )
)
AUDIO_SUFFIXES = {
    ".wav",
    ".wave",
    ".mp3",
    ".flac",
    ".ogg",
    ".opus",
    ".aac",
    ".m4a",
    ".aif",
    ".aiff",
    ".wma",
    ".au",
    ".pcm",
    ".raw",
    ".caf",
    ".amr",
    ".mid",
    ".midi",
}


def git(root: str | Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(root), *args],
        capture_output=True,
        check=True,
    )
    return result.stdout


def require_own_toplevel(
    path: str | Path, *, toplevel: str | Path | None = None
) -> Path:
    """Raise ValueError if path is not an isolated Git root."""
    root = Path(path).resolve()
    try:
        top = (
            Path(toplevel)
            if toplevel is not None
            else Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel")).strip())
        )
        if (
            not root.is_dir()
            or root != top.resolve()
            or any((parent / ".git").exists() for parent in root.parents)
        ):
            raise ValueError("Not an independent Git root")
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("Not an independent Git root") from error
    return root


def scan_text(text: str, denylist: Iterable[str]) -> Counter[str]:
    findings: Counter[str] = Counter()
    folded = text.casefold()
    findings["denylist"] = sum(folded.count(term) for term in denylist)
    findings["private_path"] = len(PRIVATE_PATH.findall(text))
    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    for token in IPV4.findall(text):
        try:
            address = ipaddress.IPv4Address(token)
        except ipaddress.AddressValueError:
            continue
        findings["network"] += any(address in net for net in PRIVATE_V4)
    for token in IPV6.findall(text):
        try:
            address = ipaddress.IPv6Address(token)
        except ipaddress.AddressValueError:
            continue
        findings["network"] += int(address) >> 121 == 126
    return +findings


def scan_data(
    data: bytes, denylist: Iterable[str], licensed_audio: Collection[str], name: str
) -> Counter[str]:
    findings = scan_text(data.decode("utf-8", errors="replace"), denylist)
    audio = (
        Path(name).suffix.lower() in AUDIO_SUFFIXES
        or (data.startswith(b"RIFF") and data[8:12] == b"WAVE")
        or data.startswith(
            (b"fLaC", b"OggS", b"ID3", b".snd", b"caff", b"#!AMR", b"MThd")
        )
        or (len(data) >= 2 and data[0] == 255 and data[1] & 224 == 224)
        or (data.startswith(b"FORM") and data[8:12] in {b"AIFF", b"AIFC"})
        or data[4:8] == b"ftyp"
    )
    if audio:
        if hashlib.sha256(data).hexdigest() not in licensed_audio:
            findings["audio"] += 1
    else:
        try:
            text = data.decode("utf-8")
            binary = any(
                not char.isprintable() and char not in "\t\r\n" for char in text
            )
        except UnicodeDecodeError:
            binary = True
        if binary:
            findings["binary"] += 1
    return +findings


def read_list(path: str | Path, *, hashes: bool = False) -> set[str]:
    entries = {
        line.strip().casefold()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if hashes:
        if any(not re.fullmatch(r"[0-9a-f]{64}", entry) for entry in entries):
            raise ValueError("Invalid audio hash list")
    elif not entries:
        raise ValueError("Empty private denylist")
    return entries


def worktree_files(root: Path) -> Iterator[tuple[str, bytes, bytes | None]]:
    names = set(git(root, "ls-files", "-co", "--exclude-standard", "-z").split(b"\0"))
    for name in sorted(names - {b""}):
        path = root / os.fsdecode(name)
        identifier = hashlib.sha256(name).hexdigest()
        if any(
            parent.is_symlink() for parent in (path, *path.parents) if parent != root
        ):
            yield identifier, name, None
        elif path.is_file():
            yield identifier, name, path.read_bytes()
        elif path.exists():
            yield identifier, name, None
        # Deleted tracked files remain in reachable history, not in the worktree.


def public_findings(
    root: Path,
    denylist: Collection[str],
    licensed_audio: Collection[str],
    commits: Iterable[str],
) -> list[tuple[str, str, Counter[str]]]:
    findings: list[tuple[str, str, Counter[str]]] = []
    for identifier, name, data in worktree_files(root):
        counts = scan_data(name, denylist, set(), "")
        counts.update(
            {"file_type": 1}
            if data is None
            else scan_data(data, denylist, licensed_audio, os.fsdecode(name))
        )
        if counts:
            findings.append(("worktree", identifier, counts))
    seen = set()
    for commit in commits:
        metadata = git(root, "cat-file", "commit", commit)
        counts = scan_data(metadata, denylist, set(), "")
        if counts:
            findings.append(("metadata", commit, counts))
        for entry in git(root, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
            if not entry:
                continue
            header, name = entry.split(b"\t", 1)
            mode, kind, oid = header.split()
            key = (mode, oid, name)
            if key in seen:
                continue
            seen.add(key)
            identifier = oid.decode("ascii")
            counts = scan_data(name, denylist, set(), "")
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                counts["file_type"] += 1
            else:
                counts.update(
                    scan_data(
                        git(root, "cat-file", "blob", identifier),
                        denylist,
                        licensed_audio,
                        os.fsdecode(name),
                    )
                )
            if counts:
                findings.append((f"blob commit={commit}", identifier, counts))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--private-denylist", type=Path, required=True)
    parser.add_argument("--licensed-audio", type=Path)
    args = parser.parse_args(argv)
    try:
        root = require_own_toplevel(args.root)
    except ValueError:
        print("FAIL own Git root count=1")
        return 1
    print("PASS own Git root")
    try:
        denylist = read_list(args.private_denylist)
        licensed_audio = (
            read_list(args.licensed_audio, hashes=True)
            if args.licensed_audio
            else set()
        )
        if args.private_denylist.resolve().is_relative_to(root):
            raise ValueError("Private denylist must be external")
        shallow = git(root, "rev-parse", "--is-shallow-repository").strip()
        if args.history and shallow != b"false":
            print("FAIL history secrets incomplete_history=1")
            return 1
        commits = (
            git(root, "rev-list", "--all").decode("ascii").splitlines()
            if args.history
            else []
        )
        if args.history:
            executable = shutil.which("gitleaks")
            if not executable:
                print("FAIL history secrets missing_tool=1")
                return 1
            result = subprocess.run(
                [
                    executable,
                    "git",
                    str(root),
                    "--redact",
                    "--exit-code",
                    "1",
                    "--no-banner",
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode:
                print("FAIL history secrets scanner_failure=1")
                return 1
            print(f"PASS history secrets commits={len(commits)}")
        findings = public_findings(root, denylist, licensed_audio, commits)
    except OSError, ValueError, UnicodeError, subprocess.CalledProcessError:
        print("FAIL public data input_or_scan_error=1")
        return 1
    if findings:
        total: Counter[str] = Counter()
        for kind, identifier, counts in findings:
            total.update(counts)
            detail = " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
            print(f"FAIL public data {kind} object={identifier} {detail}")
        print(f"FAIL public data objects={len(findings)} matches={sum(total.values())}")
        return 1
    print("PASS public data matches=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
