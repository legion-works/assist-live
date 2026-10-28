import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.corpus import load_corpus
from tools.test_environment import URL, run_live

CONTRACT = {
    "announce": (
        "Play the announcement without an STT turn.",
        "HA service result and device playback events.",
        "automated",
    ),
    "start_conversation": (
        "Play the prompt and open the microphone.",
        "HA service result, playback, and microphone events.",
        "automated",
    ),
    "ask_question": (
        "Play the question and return the selected answer.",
        "HA service response and follow-up turn events.",
        "automated",
    ),
    "timers": (
        "Start, finish, and cancel a native HA timer.",
        "Native timer callbacks and device alarm playback.",
        "automated",
    ),
    "stop_word": (
        "Stop the spoken reply on the selected stop word.",
        "Device playback end after the stop word.",
        "operator",
    ),
    "media_player": (
        "Play and stop media through the native media player.",
        "Public media-player state and device playback.",
        "automated",
    ),
    "ducking": (
        "Reduce media volume during voice and restore it after voice.",
        "Operator compares media volume before, during, and after voice.",
        "operator",
    ),
    "wake_selection": (
        "Use the selected wake word, not the previous one.",
        "Public selection state and device wake events.",
        "automated",
    ),
    "mute": (
        "Block microphone input while mute is on.",
        "Operator checks the physical mute control and microphone response.",
        "operator",
    ),
    "leds": (
        "Keep the native light patterns for each voice state.",
        "Operator observes the physical LEDs during wake, voice, reply, and mute.",
        "operator",
    ),
    "area": (
        "Resolve an unnamed target in the satellite area.",
        "Selected intent and exact virtual-target operation log.",
        "automated",
    ),
    "exposure": (
        "Refuse a target that Assist does not expose.",
        "HA intent error and completed empty operation log.",
        "automated",
    ),
    "agent_selection": (
        "Use the conversation agent selected by the pipeline.",
        "Public pipeline configuration and agent response.",
        "automated",
    ),
    "follow_up": (
        "Open a new voice turn for a continued conversation.",
        "Microphone event and parent turn identifier.",
        "automated",
    ),
    "pipeline_selection": (
        "Use the pipeline selected for the satellite.",
        "Public selection state and selected engine records.",
        "automated",
    ),
    "debug_availability": (
        "Keep the native HA pipeline debug view available.",
        "Operator opens the native HA pipeline debug view.",
        "operator",
    ),
}
PATHS = ("server_present", "server_absent")


def validate_native_matrix(rows: list[dict[str, Any]]) -> list[str]:
    failures = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            failures.append("invalid native row")
            continue
        key = (row.get("function"), row.get("path"))
        if not all(isinstance(value, str) for value in key):
            failures.append("invalid native identity")
            continue
        if key in seen:
            failures.append(f"duplicate native observation: {key}")
        seen.add(key)
        if key[0] not in CONTRACT or key[1] not in PATHS:
            failures.append(f"unknown native observation: {key}")
            continue
        expected, method, observer = CONTRACT[key[0]]
        if (row.get("expected"), row.get("method"), row.get("observer")) != (
            expected,
            method,
            observer,
        ):
            failures.append(f"changed native contract: {key}")
        observation = row.get("observation")
        try:
            timed = (
                isinstance(observation, dict)
                and datetime.fromisoformat(observation.get("observed_at", "")).tzinfo
                is not None
            )
        except ValueError, TypeError:
            timed = False
        if not isinstance(observation, dict) or not all(
            isinstance(observation.get(field), str) and observation[field].strip()
            for field in ("detail", "evidence", "observed_at")
        ):
            failures.append(f"missing native observation: {key}")
        elif not timed or (
            observation.get("passed") is not True
            or observation.get("observer") != observer
            or observation.get("scope") != "device"
        ):
            failures.append(f"unsatisfactory native observation: {key}")
    for function in CONTRACT:
        for path in PATHS:
            if (function, path) not in seen:
                failures.append(f"missing native row: {function}/{path}")
    return failures


def run_native_matrix() -> list[dict[str, Any]]:
    source = os.environ.get("ASSIST_LIVE_NATIVE_OBSERVATIONS")
    observations = json.loads(Path(source).read_text()) if source else []
    if not isinstance(observations, list):
        raise ValueError("native observations require a JSON list")
    rows = [
        {
            "function": function,
            "path": path,
            "expected": expected,
            "method": method,
            "observer": observer,
            "observation": None,
        }
        for function, (expected, method, observer) in CONTRACT.items()
        for path in PATHS
    ]
    keys = {(row["function"], row["path"]) for row in rows}
    seen = set()
    for observation in observations:
        key = (observation["function"], observation["path"])
        if key not in keys or key in seen:
            raise ValueError("unknown or duplicate native observation")
        seen.add(key)
        for row in rows:
            if (row["function"], row["path"]) == key:
                row["observation"] = observation.get("observation")
    return rows


async def run_virtual(token_file: Path, labels: Path) -> list[dict[str, Any]]:
    if URL != "http://127.0.0.1:18123":
        raise ValueError("virtual checks require the isolated loopback test instance")
    token = json.loads(token_file.read_text())["token"]
    if not isinstance(token, str) or not token:
        raise ValueError("test authentication unavailable")
    timers = [
        row for row in load_corpus(labels) if row["intent"]["name"] == "HassStartTimer"
    ]
    if not timers:
        raise ValueError("timer label unavailable")
    async with aiohttp.ClientSession(
        headers={"Authorization": "Bearer " + token}
    ) as session:
        async with session.get(URL + "/api/config") as response:
            config = await response.json()
            if response.status != 200 or config.get("version") != "2026.10.3":
                raise ValueError("isolated test instance requires HA 2026.10.3")
        async with session.get(URL + "/api/states") as response:
            states = await response.json()
        targets = {
            "light.virtual_light",
            "fan.virtual_fan",
            "input_boolean.test_light",
            "input_boolean.test_fan",
        }
        before = {
            state["entity_id"]: state["state"]
            for state in states
            if state["entity_id"] in targets
        }
        device_id = next(
            state["attributes"]["virtual_device_id"]
            for state in states
            if state["entity_id"] == "conversation.assist_live_short"
        )
        try:
            failures = await run_live(
                {"short": timers[:1], "long": timers[1:2]}, token_file, None
            )
        finally:
            async with session.post(
                URL + "/api/conversation/process",
                json={
                    "text": "cancel all timers",
                    "language": "en",
                    "agent_id": "conversation.home_assistant",
                    "device_id": device_id,
                },
            ) as response:
                cleanup_status = response.status
                await response.read()
            async with session.get(URL + "/api/states") as response:
                states = await response.json()
            after = {
                state["entity_id"]: state["state"]
                for state in states
                if state["entity_id"] in targets
            }
        results = [
            {
                "function": "timers",
                "scope": "virtual",
                "passed": not failures and cleanup_status == 200 and before == after,
                "detail": "Native timer callback and labelled interval through "
                "the public conversation API.",
                "failures": failures,
                "state_before": before,
                "state_after": after,
                "cleanup_http_status": cleanup_status,
            }
        ]
        async with session.post(
            URL + "/api/services/assist_satellite/announce",
            json={
                "entity_id": "light.virtual_light",
                "message": "Synthetic test announcement.",
            },
        ) as response:
            status = response.status
            await response.read()
        results.append(
            {
                "function": "announce",
                "scope": "virtual",
                "passed": False,
                "status": "unavailable",
                "http_status": status,
                "detail": "No voice satellite is available. "
                "The HTTP response is not an announcement observation.",
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--virtual", action="store_true")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--labels", type=Path)
    args = parser.parse_args(argv)
    try:
        document = args.matrix.read_text()
        if any(f"`{function}`" not in document for function in CONTRACT):
            raise ValueError("native matrix document is incomplete")
        if args.virtual:
            if args.token_file is None or args.labels is None:
                raise ValueError("virtual checks require --token-file and --labels")
            rows = asyncio.run(run_virtual(args.token_file, args.labels))
            failures = [row["function"] for row in rows if not row["passed"]]
            print("NOTE virtual observations do not satisfy the device matrix")
        else:
            rows = run_native_matrix()
            failures = validate_native_matrix(rows)
        args.output.write_text(json.dumps(rows, indent=2) + "\n")
        for failure in failures:
            print("FAIL " + failure)
        if not failures:
            print("PASS virtual subset" if args.virtual else "PASS native matrix")
        return int(args.check and bool(failures))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        aiohttp.ClientError,
        TimeoutError,
    ) as error:
        print(f"FAIL native matrix: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
