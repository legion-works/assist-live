"""Probe Home Assistant pipeline operations against a test instance."""

import argparse
import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

Command = Callable[..., Awaitable[Any]]
ROOT = Path(__file__).resolve().parents[1]


def pipeline_fields(
    stt_engine: str, tts_engine: str, tts_voice: str | None = None
) -> dict[str, Any]:
    return {
        "name": "Pipeline contract probe",
        "language": "en",
        "conversation_engine": "conversation.home_assistant",
        "conversation_language": "en",
        "stt_engine": stt_engine,
        "stt_language": "en",
        "tts_engine": tts_engine,
        "tts_language": "en",
        "tts_voice": tts_voice,
        "wake_word_entity": None,
        "wake_word_id": None,
        "prefer_local_intents": False,
    }


def validate_report(path: Path) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8")
        blocks = re.findall(r"```json\n(.*?)\n```", content, flags=re.DOTALL)
        if len(blocks) != 1:
            return ["report: expected one JSON contract"]
        data = json.loads(blocks[0])
        if not isinstance(data, dict):
            return ["report: expected a JSON object"]
    except OSError, ValueError:
        return ["report: cannot read JSON contract"]

    errors = []
    for key, expected in {
        "ha_version": "2026.10.3",
        "setup": "automatic",
        "public_criterion": "component __all__ export",
        "exports": ["async_create_default_pipeline", "async_update_pipeline"],
        "live": {
            "create": True,
            "get": True,
            "update": True,
            "voice_round_trip": True,
            "cleanup": True,
            "preferred_unchanged": True,
        },
        "satellite_selection": "HA harness: two select entities",
        "duplicate_repair": "required: not implemented",
    }.items():
        if data.get(key) != expected:
            errors.append(f"{key}: contract does not match the checked result")
    expected_fields = pipeline_fields("stt.test_one", "tts.test_one", "voice-two")
    if data.get("pipeline_fields") != expected_fields:
        errors.append(
            "pipeline_fields: missing or incorrect engine, language, or voice"
        )
    for heading in ("## Automatic setup", "## Manual fallback", "## Sources"):
        if heading not in content:
            errors.append(f"report: missing {heading}")
    return errors


async def probe_cycle(
    command: Command, stt_engine: str, tts_engine: str, tts_voice: str
) -> list[str]:
    before = await command("assist_pipeline/pipeline/list")
    voices = await command("tts/engine/voices", engine_id=tts_engine, language="en")
    if not any(v["voice_id"] == tts_voice for v in voices.get("voices") or []):
        raise ValueError("selected voice is not in the engine voice list")
    created = []
    errors = []
    try:
        for number in (1, 2):
            fields = pipeline_fields(stt_engine, tts_engine, tts_voice)
            fields["name"] += f" {uuid4().hex} {number}"
            pipeline = await command("assist_pipeline/pipeline/create", **fields)
            created.append(pipeline["id"])
            found = await command(
                "assist_pipeline/pipeline/get", pipeline_id=pipeline["id"]
            )
            if any(found.get(key) != value for key, value in fields.items()):
                errors.append("create/get: pipeline fields differ")
            await command(
                "assist_pipeline/pipeline/update",
                pipeline_id=pipeline["id"],
                **{**fields, "tts_voice": None},
            )
            found = await command(
                "assist_pipeline/pipeline/get", pipeline_id=pipeline["id"]
            )
            if found["tts_voice"] is not None:
                errors.append("update/get: voice did not clear")
            await command(
                "assist_pipeline/pipeline/update",
                pipeline_id=pipeline["id"],
                **fields,
            )
            found = await command(
                "assist_pipeline/pipeline/get", pipeline_id=pipeline["id"]
            )
            if any(found.get(key) != value for key, value in fields.items()):
                errors.append("update/get: pipeline fields differ")
    finally:
        for pipeline_id in created:
            try:
                await command(
                    "assist_pipeline/pipeline/delete", pipeline_id=pipeline_id
                )
            except ValueError, aiohttp.ClientError, TimeoutError:
                errors.append("cleanup: pipeline delete failed")
        after = await command("assist_pipeline/pipeline/list")
        if {p["id"] for p in before["pipelines"]} != {
            p["id"] for p in after["pipelines"]
        }:
            errors.append("cleanup: pipeline IDs differ")
        if before["preferred_pipeline"] != after["preferred_pipeline"]:
            errors.append("preferred pipeline changed")
        for error in errors:
            print(f"FAIL {error}")
    return errors


async def run_live(
    url: str, token_file: Path, stt_engine: str, tts_engine: str, tts_voice: str
) -> list[str]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 18123
        or parsed.path not in ("", "/")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("live probe requires the isolated loopback test instance")
    token = json.loads(token_file.read_text(encoding="utf-8"))["token"]
    if not isinstance(token, str) or not token:
        raise ValueError("token file requires a token string")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url.rstrip("/") + "/api/websocket") as ws:
            async with asyncio.timeout(15):
                greeting = await ws.receive_json()
                if greeting.get("ha_version") != "2026.10.3":
                    raise ValueError("live probe requires HA 2026.10.3")
                await ws.send_json({"type": "auth", "access_token": token})
                if (await ws.receive_json()).get("type") != "auth_ok":
                    raise ValueError("WebSocket authentication failed")
            counter = 0

            async def command(kind: str, **fields: Any) -> Any:
                nonlocal counter
                counter += 1
                async with asyncio.timeout(15):
                    await ws.send_json({"id": counter, "type": kind, **fields})
                    while True:
                        response = await ws.receive_json()
                        if response.get("id") == counter:
                            if not response.get("success"):
                                raise ValueError(f"WebSocket command failed: {kind}")
                            return response.get("result")

            return await probe_cycle(command, stt_engine, tts_engine, tts_voice)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--report", type=Path, default=ROOT / "docs/research/q2-pipelines.md"
    )
    parser.add_argument("--url")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--stt-engine")
    parser.add_argument("--tts-engine")
    parser.add_argument("--tts-voice")
    args = parser.parse_args(argv)
    if args.check:
        errors = validate_report(args.report)
        for error in errors:
            print(f"FAIL {error}")
        if not errors:
            print("PASS Q2 pipeline contract")
        return int(bool(errors))
    if not all(
        (args.url, args.token_file, args.stt_engine, args.tts_engine, args.tts_voice)
    ):
        parser.error(
            "live mode requires --url, --token-file, --stt-engine, "
            "--tts-engine, --tts-voice"
        )
    try:
        errors = asyncio.run(
            run_live(
                args.url,
                args.token_file,
                args.stt_engine,
                args.tts_engine,
                args.tts_voice,
            )
        )
    except (OSError, ValueError, KeyError, aiohttp.ClientError, TimeoutError) as error:
        print(f"FAIL live probe: {type(error).__name__}: {error}")
        return 1
    if not errors:
        print("PASS live HA 2026.10.3 pipeline create/get/update with non-null voice")
        print(
            "PASS cleanup: two probe pipelines deleted; "
            "pipeline IDs and preferred unchanged"
        )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
