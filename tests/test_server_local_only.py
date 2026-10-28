import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from assist_live_client import AssistLiveClient
from assist_live_client.protocol import encode_audio, encode_control
from homeassistant.components.tts import TTSAudioRequest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_stt_forward import Fallback as STTFallback
from test_stt_forward import audio, metadata
from test_stt_forward import entity as stt_entity
from test_tts_forward import Fallback as TTSFallback
from test_tts_forward import entity as tts_entity
from test_tts_forward import text

from custom_components.assist_live import Runtime
from custom_components.assist_live.diagnostics import async_get_config_entry_diagnostics


@pytest.mark.parametrize("part", ["stt", "tts"])
@pytest.mark.parametrize("local_only", [True, False], ids=["on", "off"])
@pytest.mark.parametrize(
    "policy,reason",
    [
        (
            {
                "local_only": True,
                "destinations": {"stt": "local", "tts": "local", "speaker": "unknown"},
            },
            None,
        ),
        (
            {"local_only": True, "destinations": {"stt": "cloud", "tts": "cloud"}},
            "server_destination_not_local",
        ),
        (
            {"local_only": True, "destinations": {"stt": "unknown", "tts": "unknown"}},
            "server_destination_not_local",
        ),
        (
            {"local_only": False, "destinations": {"stt": "local", "tts": "local"}},
            "server_not_local_only",
        ),
        ({}, "server_not_local_only"),
        (
            {"local_only": True, "destinations": {"speaker": "local"}},
            "server_destination_not_local",
        ),
        (
            {"local_only": None, "destinations": {"stt": "local", "tts": "local"}},
            "server_not_local_only",
        ),
        (
            {"destinations": {"stt": "local", "tts": "local"}},
            "server_not_local_only",
        ),
        (
            {
                "local_only": True,
                "destinations": {"stt": "local", "tts": "local", "speaker": "cloud"},
            },
            None,
        ),
        ({"local_only": True}, "server_destination_not_local"),
        ({"local_only": True, "destinations": None}, "server_destination_not_local"),
    ],
    ids=[
        "local",
        "cloud",
        "unknown",
        "false",
        "older",
        "missing_part",
        "null",
        "missing_state",
        "cloud_speaker",
        "missing_map",
        "null_map",
    ],
)
async def test_selection_through_pinned_health_decoder(
    hass, socket_enabled, caplog, part, local_only, policy, reason
):
    calls = []

    async def health(request):
        return web.json_response(
            {
                "version": 1,
                "healthy": True,
                "capabilities": {"stt": True, "tts": True},
                **policy,
            }
        )

    async def accepted(request):
        return web.json_response({})

    async def stream(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hello = await ws.receive_json()
        turn_id = hello["turn_id"]
        calls.append(part)
        await ws.send_json(encode_control("negotiated", turn_id, capabilities=[part]))
        if part == "stt":
            await ws.send_json(
                encode_control("final", turn_id, text="synthetic server command")
            )
        else:
            await ws.send_json(
                encode_control(
                    "format",
                    turn_id,
                    codec="pcm_s16le",
                    rate=16000,
                    channels=1,
                    width=2,
                )
            )
            await ws.send_bytes(encode_audio(turn_id, 0, b"\x01\x00"))
            await ws.send_json(encode_control("sentence_end", turn_id, sentence=0))
        await ws.send_json(encode_control("end", turn_id))
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/v1/turns", accepted)
    app.router.add_post("/v1/event", accepted)
    app.router.add_get("/v1/audio", stream)
    async with (
        TestServer(app) as server,
        AssistLiveClient(str(server.make_url("")), "test-only") as client,
    ):
        entry = MockConfigEntry(
            domain="assist_live",
            data={"satellites": {}, "local_only": not local_only},
            options={"local_only": local_only},
        )
        runtime = Runtime(hass, entry, client)
        hass.data["assist_live"] = {entry.entry_id: runtime}
        runtime.ledger.save = None

        async def fallback_policy(entity_id):
            return "local"

        runtime.ledger.fallback_policy = fallback_policy
        selected = not local_only or reason is None
        if part == "stt":
            fallback = STTFallback()
            forward = stt_entity(client, runtime.ledger, fallback)
            try:
                result = await forward.async_process_audio_stream(
                    metadata(), audio([bytes(320)])
                )
                assert result.text == (
                    "synthetic server command" if selected else "test command"
                )
            finally:
                await forward.async_will_remove_from_hass()
        else:
            fallback = TTSFallback()
            response = await tts_entity(
                client, runtime.ledger, fallback
            ).async_stream_tts_audio(
                TTSAudioRequest("en", {"voice": "voice-2"}, text())
            )
            output = b"".join([chunk async for chunk in response.data_gen])
            assert (output[44:] == b"\x01\x00") is selected
        assert calls == ([part] if selected else [])
        record = runtime.ledger.latest["satellite-a"]
        assert ("fallback_used" in record.stages) is (not selected)
        if not selected:
            assert record.timings["fallback_used"]["reason"] == reason
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert diagnostics["server_local_only"] == (
            policy["local_only"]
            if policy.get("local_only") is not None
            else "not_reported"
        )
        assert diagnostics["server_destinations"] == (
            policy["destinations"]
            if policy.get("destinations") is not None
            else "not_reported"
        )
        assert "synthetic server command" not in caplog.text
        assert "First sentence" not in caplog.text


async def test_health_not_reported_before_first_request(hass, client):
    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    hass.data["assist_live"] = {entry.entry_id: runtime}
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["server_local_only"] == "not_reported"
    assert diagnostics["server_destinations"] == "not_reported"
