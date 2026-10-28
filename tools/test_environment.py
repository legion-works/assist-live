import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import aiohttp
import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
CONTAINER = "assist-live-ha-test"
URL = "http://127.0.0.1:18123"
DEVICE_ID = "assist_live_virtual"


def validate_run_evidence(
    record: dict[str, Any], *, require_bridge: bool = False
) -> list[str]:
    failures = []
    for key in (
        "recognized_transcript",
        "selected_intent",
        "context_id",
        "response_type",
        "actions",
        "service_calls",
        "reply",
        "text_deltas",
        "group",
    ):
        if key not in record:
            failures.append(f"missing {key}")
    if failures:
        return failures
    if (
        not isinstance(record["recognized_transcript"], str)
        or not record["recognized_transcript"].strip()
    ):
        failures.append("recognized transcript")
    selected = record["selected_intent"]
    if (
        not isinstance(selected, dict)
        or not isinstance(selected.get("name"), str)
        or not selected["name"]
        or not isinstance(selected.get("slots"), dict)
    ):
        failures.append("selected intent")
        selected = {"name": None, "slots": {}}
    try:
        timer_seconds = sum(
            int(selected["slots"].get(key, 0)) * factor
            for key, factor in (("hours", 3600), ("minutes", 60), ("seconds", 1))
        )
    except ValueError, TypeError, OverflowError:
        timer_seconds = None
    if record["response_type"] != "action_done" or not record["context_id"]:
        failures.append("unsatisfactory response")
    actions = record["actions"]
    if not isinstance(actions, list) or (
        not actions
        and not (record.get("completed") is True and record.get("operations") == [])
    ):
        failures.append("missing action record")
    else:
        for action in actions:
            if not isinstance(action, dict):
                failures.append("invalid action record")
            elif action.get("kind") == "timer":
                if (
                    action.get("event") != "started"
                    or type(action.get("seconds")) is not int
                    or action["seconds"] <= 0
                    or selected["name"] != "HassStartTimer"
                    or action["seconds"] != timer_seconds
                ):
                    failures.append("unsatisfactory timer operation")
            elif action.get("kind") == "service":
                if (
                    not all(
                        action.get(key) for key in ("domain", "service", "context_id")
                    )
                    or action.get("context_id") != record["context_id"]
                    or not isinstance(action.get("service_data"), dict)
                    or not isinstance(record["service_calls"], list)
                    or {key: value for key, value in action.items() if key != "kind"}
                    not in record["service_calls"]
                ):
                    failures.append("unsatisfactory service operation")
            elif action.get("kind") == "delayed_action":
                if (
                    action.get("event") != "scheduled"
                    or not isinstance(action.get("command"), str)
                    or not action["command"].strip()
                    or type(action.get("seconds")) is not int
                    or action["seconds"] <= 0
                    or selected["name"] != "HassStartTimer"
                    or action["seconds"] != timer_seconds
                    or action["command"]
                    != selected["slots"].get("conversation_command")
                ):
                    failures.append("unsatisfactory delayed operation")
            else:
                failures.append("unknown action record")
    calls = record["service_calls"]
    if not isinstance(calls, list) or any(
        not isinstance(call, dict) or call.get("context_id") != record["context_id"]
        for call in calls
    ):
        failures.append("public service-call records")
    reply, deltas = record["reply"], record["text_deltas"]
    if not isinstance(reply, str) or not reply:
        failures.append("missing reply")
    elif record["group"] == "short":
        if len(reply) > 60 or deltas != []:
            failures.append("short reply must not stream")
    elif record["group"] == "long":
        if (
            len(reply) <= 60
            or not isinstance(deltas, list)
            or len(deltas) < 2
            or any(not isinstance(delta, str) or not delta for delta in deltas)
            or "".join(deltas) != reply
        ):
            failures.append("long reply must stream text deltas")
    else:
        failures.append("unknown reply group")
    if require_bridge or record.get("turn_id"):
        from tools.device_replay import BRIDGE_FIELDS, BRIDGE_OPERATION_MAP

        for field in BRIDGE_FIELDS:
            if field not in record:
                failures.append("missing bridge " + field)
        if any(
            not isinstance(record.get(key), str) or not record[key].strip()
            for key in ("turn_id", "run_id", "baseline_configuration_id")
        ):
            failures.append("missing turn link or run identity")
        if record.get("turn_type") != "voice" or not record.get("device_id"):
            failures.append("missing voice turn link")
        if record.get("completed") is not True:
            failures.append("incomplete recorder")
        operations = record.get("operations")
        if not isinstance(operations, list):
            failures.append("missing bridge operations")
        else:
            for operation in operations:
                if (
                    not isinstance(operation, dict)
                    or set(operation) != set(BRIDGE_OPERATION_MAP)
                    or not isinstance(operation.get("service"), str)
                    or not operation["service"]
                    or not isinstance(operation.get("target"), str)
                    or not operation["target"]
                    or not isinstance(operation.get("arguments"), dict)
                    or type(operation.get("t_monotonic_ns")) is not int
                    or operation["t_monotonic_ns"] < 0
                ):
                    failures.append("invalid bridge operation")
            observed: list[dict[str, Any]] = []
            for action in actions if isinstance(actions, list) else []:
                if not isinstance(action, dict):
                    continue
                if action.get("kind") == "service" and isinstance(
                    action.get("service_data"), dict
                ):
                    arguments = dict(action["service_data"])
                    targets = arguments.pop("entity_id", [])
                    if isinstance(targets, str):
                        targets = [targets]
                    if isinstance(targets, list):
                        observed.extend(
                            {
                                "service": str(action.get("domain"))
                                + "."
                                + str(action.get("service")),
                                "target": target,
                                "arguments": arguments,
                            }
                            for target in targets
                        )
                elif action.get("kind") in ("timer", "delayed_action"):
                    arguments = {"seconds": action.get("seconds")}
                    if action["kind"] == "delayed_action":
                        arguments["command"] = action.get("command")
                    observed.append(
                        {
                            "service": "timer.scheduled"
                            if action["kind"] == "delayed_action"
                            else "timer." + str(action.get("event")),
                            "target": DEVICE_ID,
                            "arguments": arguments,
                        }
                    )
            if all(isinstance(item, dict) for item in operations) and Counter(
                json.dumps(
                    {key: item.get(key) for key in ("service", "target", "arguments")},
                    sort_keys=True,
                )
                for item in operations
            ) != Counter(json.dumps(item, sort_keys=True) for item in observed):
                failures.append("operations disagree with public action records")
    return failures


def expected_operations(
    label: dict[str, Any], inventory: dict[str, Any]
) -> list[dict[str, Any]] | None:
    intent = label["intent"]
    entry = inventory.get(intent["name"])
    if not isinstance(entry, dict):
        return None
    slots = intent["slots"]
    if entry.get("operation") == "timer":
        if len(entry.get("entities", [])) != 1:
            return None
        try:
            seconds = sum(
                int(slots.get(key, 0)) * factor
                for key, factor in (("hours", 3600), ("minutes", 60), ("seconds", 1))
            )
        except ValueError, TypeError, OverflowError:
            return None
        if seconds <= 0:
            return None
        arguments: dict[str, Any] = {"seconds": seconds}
        service = "timer.started"
        if slots.get("conversation_command"):
            service = "timer.scheduled"
            arguments["command"] = slots["conversation_command"]
        return [
            {"service": service, "target": entry["entities"][0], "arguments": arguments}
        ]
    if entry.get("operation") != "service" or intent["name"] not in (
        "HassTurnOn",
        "HassTurnOff",
    ):
        return None
    name = str(slots.get("name", "")).casefold().replace("_", " ")
    targets = [
        target
        for target in entry.get("entities", [])
        if name
        and name
        in (
            target,
            target.split(".")[-1].replace("_", " "),
            target.split(".")[0],
            "test " + target.split(".")[0],
        )
    ]
    if len(targets) != 1:
        return None
    target = targets[0]
    return [
        {
            "service": target.split(".")[0]
            + (".turn_on" if intent["name"] == "HassTurnOn" else ".turn_off"),
            "target": target,
            "arguments": {},
        }
    ]


def validate_intent_action_evidence(
    record: dict[str, Any], label: dict[str, Any]
) -> list[str]:
    failures = []
    if (record.get("selected_intent") or {}).get("name") != label["intent"]["name"]:
        failures.append("intent differs")
    expected_seconds = sum(
        int(label["intent"]["slots"].get(key, 0)) * factor
        for key, factor in (("hours", 3600), ("minutes", 60), ("seconds", 1))
    )
    if (
        "operations" not in record
        and label["intent"]["name"] == "HassStartTimer"
        and not any(
            action.get("kind") == "timer" and action.get("seconds") == expected_seconds
            for action in record["actions"]
        )
    ):
        failures.append("timer duration differs")
    if "operations" in record:
        inventory = json.loads((FIXTURES / "intents.json").read_text())
        expected = expected_operations(label, inventory)
        if expected is None:
            failures.append("operation expectation unavailable")
        else:
            actual = record["operations"]
            if not isinstance(actual, list) or any(
                not isinstance(item, dict) for item in actual
            ):
                failures.append("operations differ")
            elif Counter(
                json.dumps(
                    {key: item.get(key) for key in ("service", "target", "arguments")},
                    sort_keys=True,
                )
                for item in actual
            ) != Counter(json.dumps(item, sort_keys=True) for item in expected):
                failures.append("operations differ")
    return failures


def reply_groups(
    rows: list[dict[str, Any]], split: str
) -> dict[str, list[dict[str, Any]]]:
    selected = sorted(
        (
            row
            for row in rows
            if row.get("split") == split and row.get("set") == "replay"
        ),
        key=lambda row: row["id"],
    )
    midpoint = len(selected) // 2
    if len({row["id"] for row in selected}) != len(selected):
        raise ValueError("duplicate replay recording ID")
    groups = {"short": selected[:midpoint], "long": selected[midpoint:]}
    if any(len(group) < 50 for group in groups.values()):
        raise ValueError("each reply group requires at least 50 replay commands")
    return groups


def validate_inventory(
    rows: list[dict[str, Any]], inventory: dict[str, Any]
) -> list[str]:
    return [
        "uncovered corpus intent"
        for row in rows
        if not isinstance(inventory.get(row["intent"]["name"]), dict)
        or not inventory[row["intent"]["name"]].get("entities")
        or not inventory[row["intent"]["name"]].get("operation")
    ]


def validate_live_inventory(
    inventory: dict[str, Any],
    states: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    device_id: str,
) -> list[str]:
    available = {state["entity_id"] for state in states}
    available.update(
        identifier
        for device in devices
        if device["id"] == device_id
        for domain, identifier in device["identifiers"]
        if domain == "assist_live_test_agent"
    )
    required = {
        target
        for description in inventory.values()
        for target in description["entities"]
    }
    return [
        f"virtual inventory target missing: {target}"
        for target in sorted(required - available)
    ]


def validate_agent_source(directory: Path) -> list[str]:
    failures = []
    for name in ("__init__.py", "conversation.py", "config_flow.py"):
        if not (directory / name).is_file():
            failures.append(f"missing agent fixture source {name}")
    for path in directory.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if any(
            term in source for term in ("debug", "trace_get", "trace/list", "trace/get")
        ):
            failures.append("agent reads a debug API")
    return failures


def install_virtual() -> None:
    mounts = json.loads(
        subprocess.check_output(
            ["docker", "inspect", CONTAINER, "--format", "{{json .Mounts}}"], text=True
        )
    )
    if not any(
        mount.get("Name") == "assist-live-ha-test-config"
        and mount.get("Destination") == "/config"
        for mount in mounts
    ):
        raise ValueError("isolated test volume is necessary")
    subprocess.run(
        ["docker", "exec", CONTAINER, "mkdir", "-p", "/config/custom_components"],
        check=True,
    )
    subprocess.run(
        [
            "docker",
            "cp",
            str(FIXTURES / "custom_components/assist_live_test_agent"),
            f"{CONTAINER}:/config/custom_components/",
        ],
        check=True,
    )
    config_bytes = subprocess.check_output(
        [
            "docker",
            "exec",
            CONTAINER,
            "python",
            "-c",
            "from pathlib import Path; "
            "print(Path('/config/configuration.yaml').read_text())",
        ]
    )
    config = yaml.safe_load(config_bytes)
    config.setdefault("homeassistant", {}).setdefault("packages", {})[
        "assist_live_virtual"
    ] = yaml.safe_load((FIXTURES / "ha-virtual.yaml").read_text())
    with tempfile.TemporaryDirectory(prefix="assist-live-test-") as directory:
        path = Path(directory) / "configuration.yaml"
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        subprocess.run(
            [
                "docker",
                "exec",
                CONTAINER,
                "python",
                "-c",
                "from pathlib import Path; p=Path('/config/configuration.yaml'); "
                "b=p.with_suffix('.before-virtual'); "
                "b.exists() or b.write_bytes(p.read_bytes())",
            ],
            check=True,
        )
        subprocess.run(
            ["docker", "cp", str(path), f"{CONTAINER}:/config/configuration.yaml"],
            check=True,
        )
    subprocess.run(
        ["docker", "restart", CONTAINER], check=True, stdout=subprocess.DEVNULL
    )


async def configure_agent(
    session: aiohttp.ClientSession, token: str, *, remove: bool = False
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with session.get(
        URL + "/api/config/config_entries/entry", headers=headers
    ) as response:
        response.raise_for_status()
        entries = [
            entry
            for entry in await response.json()
            if entry["domain"] == "assist_live_test_agent"
        ]
    if len(entries) > 1:
        raise ValueError("duplicate virtual agent entries")
    if remove:
        for entry in entries:
            async with session.delete(
                URL + "/api/config/config_entries/entry/" + entry["entry_id"],
                headers=headers,
            ) as response:
                response.raise_for_status()
        return
    if entries:
        return
    async with session.post(
        URL + "/api/config/config_entries/flow",
        headers=headers,
        json={"handler": "assist_live_test_agent", "show_advanced_options": False},
    ) as response:
        response.raise_for_status()
        result = await response.json()
        if result.get("type") != "create_entry":
            raise ValueError("test agent config flow did not make an entry")


async def run_live(
    groups: dict[str, list[dict[str, Any]]],
    token_file: Path,
    output: Path | None,
    *,
    remove: bool = False,
) -> list[str]:
    token = json.loads(token_file.read_text(encoding="utf-8"))["token"]
    if not isinstance(token, str) or not token:
        raise ValueError("token file requires a token string")
    failures: list[str] = []
    records: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        for attempt in range(90):
            try:
                ws = await session.ws_connect(URL + "/api/websocket")
                break
            except aiohttp.ClientError:
                if attempt == 89:
                    raise ValueError("isolated test instance unavailable") from None
                await asyncio.sleep(1)
        async with ws:
            async with asyncio.timeout(15):
                greeting = await ws.receive_json()
                if greeting.get("ha_version") != "2026.10.3":
                    raise ValueError("isolated test instance requires HA 2026.10.3")
                await ws.send_json({"type": "auth", "access_token": token})
                if (await ws.receive_json()).get("type") != "auth_ok":
                    raise ValueError("test authentication failed")
            await configure_agent(session, token, remove=remove)
            if remove:
                return []
            counter = 0
            pending: list[dict[str, Any]] = []

            async def command(kind: str, **fields: Any) -> Any:
                nonlocal counter
                counter += 1
                async with asyncio.timeout(30):
                    await ws.send_json({"id": counter, "type": kind, **fields})
                    while True:
                        response = await ws.receive_json()
                        if response.get("type") == "event":
                            event = response.get("event", {})
                            if event.get("event_type") == "assist_live_test_record":
                                pending.append(event["data"])
                        if (
                            response.get("id") == counter
                            and response.get("type") == "result"
                        ):
                            if not response.get("success"):
                                code = response.get("error", {}).get("code", "unknown")
                                raise ValueError(
                                    f"public command failed: {kind} ({code})"
                                )
                            return response.get("result")

            for attempt in range(30):
                try:
                    agents = await command("conversation/agent/list")
                except ValueError as error:
                    if "unknown_command" not in str(error):
                        raise
                    agents = {"agents": []}
                if {
                    "conversation.assist_live_short",
                    "conversation.assist_live_long",
                } <= {agent["id"] for agent in agents["agents"]}:
                    break
                if attempt == 29:
                    raise ValueError("virtual conversation agents unavailable")
                await asyncio.sleep(1)
            states = await command("get_states")
            device_id = next(
                state["attributes"]["virtual_device_id"]
                for state in states
                if state["entity_id"] == "conversation.assist_live_short"
            )
            inventory_errors = validate_live_inventory(
                json.loads((FIXTURES / "intents.json").read_text()),
                states,
                await command("config/device_registry/list"),
                device_id,
            )
            if inventory_errors:
                raise ValueError("; ".join(inventory_errors))
            visible = {"light.virtual_light", "fan.virtual_fan"}
            if not visible <= {state["entity_id"] for state in states}:
                raise ValueError("virtual entity inventory missing")
            await command(
                "homeassistant/expose_entity",
                assistants=["conversation"],
                entity_ids=[state["entity_id"] for state in states],
                should_expose=False,
            )
            await command(
                "homeassistant/expose_entity",
                assistants=["conversation"],
                entity_ids=sorted(visible),
                should_expose=True,
            )
            await command("subscribe_events", event_type="assist_live_test_record")
            for phase in ("P0", "P1"):
                for group, rows in groups.items():
                    for row in rows:
                        before = len(pending)
                        result = await command(
                            "conversation/process",
                            text=row["transcript"],
                            language="en",
                            agent_id=f"conversation.assist_live_{group}",
                            device_id=device_id,
                        )
                        # A public command drains events queued before its result.
                        await command("conversation/agent/list")
                        if len(pending) != before + 1:
                            failures.append(f"{row['id']}: missing agent record")
                            continue
                        record = pending[-1]
                        record.update(
                            recording_id=row["id"],
                            phase=phase,
                            run_id="text-validation",
                            baseline_configuration_id="text-validation",
                        )
                        errors = validate_run_evidence(record)
                        errors += validate_intent_action_evidence(record, row)
                        if (
                            result["response"]["speech"]["plain"]["speech"]
                            != record["reply"]
                        ):
                            errors.append("reply differs")
                        failures.extend(f"{row['id']}: {error}" for error in errors)
                        records.append(record)
                await command(
                    "conversation/process",
                    text="cancel all timers",
                    language="en",
                    agent_id="conversation.home_assistant",
                    device_id=device_id,
                )
            for row_id in {record["recording_id"] for record in records}:
                pair = [
                    record for record in records if record["recording_id"] == row_id
                ]
                if len(pair) != 2 or any(
                    pair[0][key] != pair[1][key]
                    for key in ("reply", "group", "text_deltas", "selected_intent")
                ):
                    failures.append(f"{row_id}: P0 and P1 differ")
    if output is not None:
        if output.resolve().is_relative_to(ROOT):
            raise ValueError("test records must stay external to the repository")
        output.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
    return failures


async def record_device(
    token_file: Path, output: Path, count: int, run_id: str, config: str
) -> list[str]:
    if count <= 0 or not run_id or not config or output.exists():
        raise ValueError(
            "new output, positive count, run ID, and baseline "
            "configuration are necessary"
        )
    if output.resolve().is_relative_to(ROOT):
        raise ValueError("test records must stay external to the repository")
    token = json.loads(token_file.read_text())["token"]
    records: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(URL + "/api/websocket") as ws:
            async with asyncio.timeout(30):
                if (await ws.receive_json()).get("ha_version") != "2026.10.3":
                    raise ValueError("isolated test instance requires HA 2026.10.3")
                await ws.send_json({"type": "auth", "access_token": token})
                if (await ws.receive_json()).get("type") != "auth_ok":
                    raise ValueError("test authentication failed")
                await ws.send_json(
                    {
                        "id": 1,
                        "type": "subscribe_events",
                        "event_type": "assist_live_test_record",
                    }
                )
                if (await ws.receive_json()).get("success") is not True:
                    raise ValueError("public recorder subscription failed")
            print("READY device operation recorder", flush=True)
            async with asyncio.timeout(count * 120):
                while len(records) < count:
                    message = await ws.receive_json()
                    if message.get("type") != "event":
                        continue
                    record = message["event"]["data"]
                    record.update(run_id=run_id, baseline_configuration_id=config)
                    failures = validate_run_evidence(record, require_bridge=True)
                    if failures:
                        return failures
                    if any(item["turn_id"] == record["turn_id"] for item in records):
                        return ["duplicate device turn"]
                    records.append(record)
                    output.write_text(
                        "".join(json.dumps(item) + "\n" for item in records)
                    )
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--split", choices=("held_out", "tuning"), default="held_out")
    parser.add_argument("--install-virtual", action="store_true")
    parser.add_argument("--uninstall-virtual", action="store_true")
    parser.add_argument("--check-evidence", action="store_true")
    parser.add_argument(
        "--token-file", type=Path, default=os.environ.get("ASSIST_LIVE_TEST_TOKEN_FILE")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--record-device", type=int, metavar="TURNS")
    args = parser.parse_args(argv)
    try:
        if args.record_device is not None:
            if args.token_file is None or args.output is None:
                raise ValueError("device recorder requires --token-file and --output")
            failures = asyncio.run(
                record_device(
                    args.token_file,
                    args.output,
                    args.record_device,
                    os.environ.get("ASSIST_LIVE_RUN_ID", ""),
                    os.environ.get("ASSIST_LIVE_BASELINE_CONFIG_ID", ""),
                )
            )
            for failure in failures:
                print("FAIL " + failure)
            return int(bool(failures))
        rows = [
            json.loads(line)
            for line in args.labels.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        failures = validate_inventory(
            rows, json.loads((FIXTURES / "intents.json").read_text())
        )
        failures += validate_agent_source(
            FIXTURES / "custom_components/assist_live_test_agent"
        )
        groups = reply_groups(rows, args.split)
        if failures:
            raise ValueError("; ".join(failures))
        if not args.check_evidence or args.token_file is None:
            raise ValueError("--check-evidence and a test token file are necessary")
        if args.install_virtual:
            install_virtual()
        failures = asyncio.run(
            run_live(
                groups, args.token_file, args.output, remove=args.uninstall_virtual
            )
        )
        if args.uninstall_virtual:
            print("PASS virtual agent entry removed")
            return 0
        for failure in failures:
            print(f"FAIL {failure}")
        if failures:
            return 1
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        aiohttp.ClientError,
        TimeoutError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"FAIL virtual test environment: {type(error).__name__}: {error}")
        return 1
    print("PASS virtual intent coverage")
    print("PASS short and long groups")
    print("PASS transcript intent action evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
