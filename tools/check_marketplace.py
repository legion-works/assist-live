import argparse
import hashlib
import html
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
RULE_URL = (
    "https://contributors.home-assistant.io/marketplace/reference/repository-structure/"
)
VALIDATION_URL = (
    "https://contributors.home-assistant.io/marketplace/reference/validation/"
)
RULE_QUOTE = (
    "The Marketplace also refuses repositories with apps for Home Assistant OS."
)
SOURCES = {
    "pypi": "https://pypi.org/pypi/homeassistant/json",
    "github": "https://api.github.com/repos/home-assistant/core/releases?per_page=100",
    "release_notes": "https://www.home-assistant.io/blog/2026/11/04/release-202611/",
}
Status = Literal["accepted", "rejected", "pending_release"]


def classify(*, released: bool, accepted: bool | None) -> Status:
    if not released:
        return "pending_release"
    if accepted is None:
        raise ValueError("validation_required: release alone is not acceptance")
    return "accepted" if accepted else "rejected"


def stable_versions(versions: list[str]) -> list[str]:
    return sorted(
        {
            v.removeprefix("v")
            for v in versions
            if re.fullmatch(r"v?\d{4}\.\d+\.\d+", v)
        },
        key=lambda v: tuple(map(int, v.split("."))),
    )


def valid_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat() == value
    except ValueError:
        return False


def validate_record(data: dict[str, Any], minimum: str) -> list[str]:
    errors = []
    if not re.fullmatch(r"\d{4}\.(?:[1-9]|1[0-2])", minimum):
        return ["minimum: expected YYYY.M or YYYY.MM without a leading zero"]
    if data.get("minimum") != minimum:
        errors.append("minimum: record does not match requested release")
    date = data.get("checked_at")
    if not valid_date(date):
        errors.append("checked_at: expected UTC access date")
    sources = data.get("sources", {})
    if not isinstance(sources, dict):
        sources = {}
    for name, url in SOURCES.items():
        source = sources.get(name, {})
        if (
            not isinstance(source, dict)
            or source.get("url") != url
            or source.get("checked_at") != date
            or source.get("http_status") not in (200, 404)
            or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", "")))
        ):
            errors.append(f"sources.{name}: missing dated official source")
    if errors:
        return errors
    for name in ("pypi", "github"):
        source = sources[name]
        releases = source.get("target_releases")
        if (
            source["http_status"] != 200
            or not isinstance(releases, list)
            or any(not isinstance(v, str) for v in releases)
            or releases != stable_versions(releases)
            or source.get("latest_stable")
            not in stable_versions([str(source.get("latest_stable"))])
        ):
            errors.append(f"sources.{name}: invalid release metadata")
    rule = data.get("rule", {})
    if (
        not isinstance(rule, dict)
        or rule.get("url") != RULE_URL
        or rule.get("checked_at") != date
        or rule.get("quote") != RULE_QUOTE
        or rule.get("state") != "documented"
    ):
        errors.append("rule: missing dated official app-repository rule")
    validator = data.get("validator", {})
    released = data.get("release_version")
    reference = (
        (validator.get("tag") if released is not None else validator.get("commit"))
        if isinstance(validator, dict)
        else None
    )
    if (
        not isinstance(validator, dict)
        or validator.get("documentation") != VALIDATION_URL
        or validator.get("checked_at") != date
        or not re.fullmatch(r"[0-9a-f]{40}", str(validator.get("commit", "")))
        or not isinstance(validator.get("source_url"), str)
        or not validator["source_url"].startswith(
            "https://github.com/home-assistant/core/blob/"
            + str(reference)
            + "/homeassistant/components/marketplace/"
        )
        or not validator.get("quote")
    ):
        errors.append("validator: missing pinned core source and documentation")
    if not isinstance(validator, dict):
        validator = {}
    validation = data.get("validation", {})
    if not isinstance(validation, dict) or set(validation) != {
        "integration",
        "app_fixture",
        "mixed_fixture",
    }:
        errors.append("validation: missing integration, app-only, or mixed records")
        validation = {}
    if released is None:
        if validator.get("tag") != "dev":
            errors.append("validator: pending observation requires the dev tag")
        if data.get("status") != "pending_release":
            errors.append("status: unreleased cannot mean accepted or rejected")
        if any(sources[n].get("target_releases") for n in ("pypi", "github")) or (
            sources["release_notes"]["http_status"] != 404
        ):
            errors.append("release_sources: disagree or require released validation")
        if any(v is not None for v in validation.values()):
            errors.append("validation: no acceptance evidence before release")
    else:
        stable_release = (
            isinstance(released, str)
            and stable_versions([released]) == [released]
            and released.startswith(minimum + ".")
        )
        if not stable_release:
            errors.append(
                "release_version: expected a stable release in requested month"
            )
        if any(
            released not in sources[n].get("target_releases", [])
            for n in ("pypi", "github")
        ):
            errors.append("release_version: not confirmed by both release sources")
        if sources["release_notes"]["http_status"] != 200:
            errors.append("release_version: official release notes unavailable")
        if validator.get("release_version") != released:
            errors.append("validator: released source must replace the dev observation")
        if not stable_release or validator.get("tag") != released:
            errors.append("validator: tag must match the stable release_version")
        if stable_release:
            for name in ("pypi", "github"):
                latest = sources[name].get("latest_stable")
                if isinstance(latest, str) and stable_versions([latest]) == [latest]:
                    if tuple(map(int, latest.split("."))) < tuple(
                        map(int, released.split("."))
                    ):
                        errors.append(
                            f"sources.{name}: latest_stable precedes release_version"
                        )
        for name in ("integration", "app_fixture", "mixed_fixture"):
            result = validation.get(name)
            if (
                not isinstance(result, dict)
                or result.get("validator") != "marketplace_install"
                or result.get("release_version") != released
                or not valid_date(result.get("checked_at"))
                or result.get("outcome") not in ("accepted", "rejected")
                or not result.get("source_record")
            ):
                errors.append(
                    f"validation.{name}: isolated Marketplace result required"
                )
        if not errors:
            expected = classify(
                released=True,
                accepted=validation["integration"]["outcome"] == "accepted",
            )
            if data.get("status") != expected:
                errors.append("status: does not match integration result")
            if validation["app_fixture"]["outcome"] != "rejected":
                errors.append("validation.app_fixture: app repository must be refused")
    return errors


def fetch(url: str) -> tuple[int, bytes]:
    request = Request(url, headers={"User-Agent": "assist-live-marketplace-probe"})
    github_api = urlsplit(url).hostname == "api.github.com"
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if github_api and urlsplit(url).scheme == "https" and token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except HTTPError as error:
        with error:
            if (
                github_api
                and error.code == 403
                and (
                    error.headers.get("X-RateLimit-Remaining") == "0"
                    or "rate limit" in str(error.reason).lower()
                )
            ):
                raise ValueError(
                    "github_rate_limit: set GITHUB_TOKEN or GH_TOKEN "
                    "and retry the explicit --live probe"
                ) from None
            if error.code != 404:
                raise
            return error.code, error.read()
    except ValueError:
        if github_api and token:
            raise ValueError(
                "github_request: request rejected; check GITHUB_TOKEN or GH_TOKEN"
            ) from None
        raise


def fetch_live(minimum: str) -> dict[str, Any]:
    if minimum != "2026.11":
        raise ValueError(
            "live_minimum: only the 2026.11 release-note URL is configured"
        )
    date = datetime.now(UTC).date().isoformat()
    record: dict[str, Any] = {
        "minimum": minimum,
        "checked_at": date,
        "status": "pending_release",
        "release_version": None,
        "sources": {},
        "validation": {"integration": None, "app_fixture": None, "mixed_fixture": None},
    }
    for name, url in SOURCES.items():
        status, body = fetch(url)
        source: dict[str, Any] = {
            "url": url,
            "checked_at": date,
            "http_status": status,
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        if name in ("pypi", "github"):
            payload = json.loads(body)
            versions = stable_versions(
                [v for v, files in payload["releases"].items() if files]
                if name == "pypi"
                else [
                    r["tag_name"]
                    for r in payload
                    if not r["draft"] and not r["prerelease"]
                ]
            )
            source["latest_stable"] = versions[-1]
            source["target_releases"] = [
                v for v in versions if v.startswith(minimum + ".")
            ]
        record["sources"][name] = source
    status, body = fetch(RULE_URL)
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body.decode())).split())
    text = re.sub(r"\s+([.,:;])", r"\1", text)
    if status != 200 or RULE_QUOTE not in text:
        raise ValueError("rule_changed: re-read the official repository rule")
    record["rule"] = {
        "url": RULE_URL,
        "checked_at": date,
        "state": "documented",
        "quote": RULE_QUOTE,
    }
    _, body = fetch("https://api.github.com/repos/home-assistant/core/commits/dev")
    commit = json.loads(body)
    sha = commit["sha"]
    directory = "homeassistant/components/marketplace/repositories"
    _, body = fetch(
        f"https://api.github.com/repos/home-assistant/core/contents/{directory}?ref={sha}"
    )
    entries = json.loads(body)
    candidates = []
    for entry in entries:
        if entry["name"].endswith(".py"):
            _, source_body = fetch(entry["download_url"])
            lines = source_body.decode().splitlines()
            for index, line in enumerate(lines):
                if "repository.yaml" in line or "repository.yml" in line:
                    candidates.append(
                        {
                            "documentation": VALIDATION_URL,
                            "checked_at": date,
                            "commit": sha,
                            "tag": "dev",
                            "commit_date": commit["commit"]["committer"]["date"],
                            "source_url": f"https://github.com/home-assistant/core/blob/{sha}/{directory}/{entry['name']}",
                            "line": index + 1,
                            "quote": "\n".join(lines[max(0, index - 4) : index + 12]),
                        }
                    )
    if not candidates:
        raise ValueError("validator_changed: no app-repository check found")
    record["validator"] = candidates[0]
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimum", default="2026.11")
    parser.add_argument(
        "--report", type=Path, default=ROOT / "docs/research/q3-marketplace.md"
    )
    parser.add_argument(
        "--live", action="store_true", help="Fetch official sources explicitly"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record-status", action="store_true")
    mode.add_argument("--require-accepted", action="store_true")
    args = parser.parse_args(argv)
    try:
        content = args.report.read_text()
        if args.live:
            data = fetch_live(args.minimum)
            print(json.dumps(data, indent=2))
        else:
            blocks = re.findall(r"```json\n(.*?)\n```", content, re.S)
            if len(blocks) != 1:
                raise ValueError("expected one JSON source record")
            data = json.loads(blocks[0])
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
        errors = validate_record(data, args.minimum)
        if args.record_status:
            section = re.search(
                r"^## Check-again condition and procedure\n(.*?)(?=^## |\Z)",
                content,
                re.M | re.S,
            )
            if section is None:
                errors.append("procedure: missing check-again section")
            else:
                procedure = section[1]
                for step in range(1, 6):
                    if not re.search(rf"^{step}\. \S", procedure, re.M):
                        errors.append(f"procedure: missing check-again step {step}")
                for marker in (
                    "first stable",
                    f"`{args.minimum}.x`",
                    "--live --record-status",
                    "homeassistant/components/marketplace/repositories/integration.py",
                    "loopback",
                    "Settings > Marketplace",
                    "uv run python tools/check_marketplace.py --minimum "
                    f"{args.minimum} --report docs/research/q3-marketplace.md "
                    "--require-accepted",
                ):
                    if marker not in procedure:
                        errors.append(f"procedure: missing {marker}")
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print(f"FAIL Q3 report: {error}")
        return 1
    for failure in errors:
        print(f"FAIL Q3 {failure}")
    if errors:
        return 1
    status = data["status"]
    if args.require_accepted:
        if status != "accepted":
            reason = (
                f"HA {args.minimum} has no recorded stable release"
                if status == "pending_release"
                else "integration refused by Marketplace"
            )
            print(f"FAIL Q3 {status}: {reason}")
            return 1
        print("PASS Q3 accepted")
    else:
        print(f"STATUS Q3 {status} (recorded observation, not acceptance)")
        print("PASS Q3 status recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
