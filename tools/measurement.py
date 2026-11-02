import asyncio
import json
import time
from pathlib import Path
from typing import Any

import aiohttp

Row = dict[str, Any]
LOSS_FLAGS = ("loss_present", "loss_unavailable", "reset_unknown", "no_device_events")


def endpoint_uncertainty(event: dict[str, Any]) -> float | None:
    value = event.get("uncertainty_ms")
    if value is not None:
        return float(value)
    if event.get("quality") == "estimate" or event.get("source") == "device":
        return None
    return 0.0


def counter(value: object = None, since: object = None, boot: object = None) -> Row:
    result: Row = {
        "value": value if type(value) is int and value >= 0 else None,
        "last_reset_utc_ns": since if type(since) is int and since > 0 else None,
    }
    if type(boot) is int and boot >= 0:
        result["boot_id"] = boot
    return result


def loss_window(start: Row, end: Row) -> Row:
    loss = unavailable = reset = False
    methods = {}
    settled_missing = {}
    no_device_events = []
    deltas: Row = {"integration": None, "server": {}, "device": {}}
    for kind in ("integration", "server", "device"):
        before = (
            {"integration": start.get(kind, {})}
            if kind == "integration"
            else start.get(kind, {})
        )
        after = (
            {"integration": end.get(kind, {})}
            if kind == "integration"
            else end.get(kind, {})
        )
        if not before or not after:
            unavailable = True
        for satellite in before.keys() | after.keys():
            left, right = before.get(satellite, {}), after.get(satellite, {})
            a, b = left.get("value"), right.get("value")
            boots = (left.get("boot_id"), right.get("boot_id"))
            valid_boots = all(type(boot) is int and boot >= 0 for boot in boots)
            same_boot = valid_boots and boots[0] == boots[1]
            if type(a) is not int or type(b) is not int or a < 0 or b < 0:
                unavailable = True
            elif b > a:
                loss = True
            elif b < a:
                if kind == "server" and same_boot:
                    loss |= b > 0
                    settled_missing[satellite] = b
                else:
                    reset = True
            epochs = (left.get("last_reset_utc_ns"), right.get("last_reset_utc_ns"))
            if kind == "server" and boots == (None, None) and a == b == 0:
                no_device_events.append(satellite)
                method = "no_device_events"
            elif kind in ("server", "device") and valid_boots:
                reset |= boots[0] != boots[1]
                method = "device_boot_id"
            elif kind != "server" and all(
                type(epoch) is int and epoch > 0 for epoch in epochs
            ):
                reset |= epochs[0] != epochs[1]
                method = "counters_since_utc_ns"
            else:
                reset = True
                method = "reset_unknown"
            methods[kind + ":" + satellite] = method
            same_epoch = method == "counters_since_utc_ns" and epochs[0] == epochs[1]
            delta = (
                b - a
                if type(a) is int
                and type(b) is int
                and a >= 0
                and b >= 0
                and (same_boot or same_epoch)
                and (b >= a or kind == "server")
                else None
            )
            if kind == "integration":
                deltas[kind] = delta
            else:
                deltas[kind][satellite] = delta
    return {
        "start": start,
        "end": end,
        "loss_present": loss,
        "counter_deltas": deltas,
        "loss_unavailable": unavailable,
        "reset_unknown": reset,
        "reset_methods": methods,
        "server_missing_after_settle": settled_missing,
        "no_device_events": no_device_events,
        "complete": not (loss or unavailable or reset or no_device_events),
    }


async def capture_loss(
    session: aiohttp.ClientSession | None,
    *,
    satellites: list[str],
    ha_url: str | None = None,
    ha_token_file: Path | None = None,
    integration_entry: str | None = None,
    device_entities: dict[str, str] | None = None,
    server_url: str | None = None,
    server_token: str | None = None,
    integration_data: Row | None = None,
) -> Row:
    result: Row = {
        "observed_utc_ns": time.time_ns(),
        "integration": counter(),
        "server": {satellite: counter() for satellite in satellites},
        "device": {satellite: counter() for satellite in satellites},
    }
    if integration_data is not None:
        result["integration"] = counter(
            integration_data.get("timing_events_dropped"),
            integration_data.get("counters_since_utc_ns"),
        )
    if session is None:
        return result
    failures = (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        TimeoutError,
        aiohttp.ClientError,
    )
    if ha_url and ha_token_file:
        try:
            token = json.loads(ha_token_file.read_text())["token"]
            headers = {"Authorization": "Bearer " + token}
            if integration_entry:
                async with asyncio.timeout(30):
                    async with session.get(
                        ha_url.rstrip("/")
                        + "/api/diagnostics/config_entry/"
                        + integration_entry,
                        headers=headers,
                    ) as response:
                        response.raise_for_status()
                        data = (await response.json())["data"]
                result["integration"] = counter(
                    data.get("timing_events_dropped"), data.get("counters_since_utc_ns")
                )
            for satellite, entity in (device_entities or {}).items():
                if satellite not in result["device"]:
                    continue
                try:
                    async with asyncio.timeout(30):
                        async with session.get(
                            ha_url.rstrip("/") + "/api/states/" + entity,
                            headers=headers,
                        ) as response:
                            response.raise_for_status()
                            data = await response.json()
                    value = data.get("state", "")
                    attributes = data.get("attributes", {})
                    result["device"][satellite] = counter(
                        int(value)
                        if isinstance(value, str) and value.isdecimal()
                        else None,
                        attributes.get("counters_since_utc_ns"),
                        attributes.get("boot_id"),
                    )
                except failures:
                    pass
        except failures:
            pass
    if server_url and server_token:
        try:
            async with asyncio.timeout(30):
                async with session.get(
                    server_url.rstrip("/") + "/health",
                    headers={"Authorization": "Bearer " + server_token},
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
            for satellite, value in data.get("sequence_gaps_by_satellite", {}).items():
                if satellite in result["server"]:
                    result["server"][satellite] = counter(
                        value.get("missing"), boot=value.get("boot_id")
                    )
        except failures:
            pass
    return result
