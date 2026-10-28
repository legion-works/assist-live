from unittest.mock import patch

import pytest
from homeassistant.core import callback
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.mark.parametrize(
    "iot_class,want",
    [
        ("local_push", "local"),
        ("local_polling", "local"),
        ("cloud_push", "cloud"),
        ("cloud_polling", "cloud"),
        (None, "unknown"),
        ("assumed_state", "unknown"),
        (123, "unknown"),
        ({"unexpected": "local"}, "unknown"),
    ],
)
def test_iot_class_is_not_assumed_local(iot_class, want):
    from custom_components.assist_live import config_flow

    assert getattr(config_flow, "destination", lambda _: "missing")(iot_class) == want


async def test_unknown_fallback_requires_explicit_audited_choice(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    settings = {
        "url": "https://server.example",
        "token": "test-only",
        "fallback_stt": "stt.standard",
        "fallback_tts": "tts.standard",
        "local_only": True,
    }
    result = await flow.async_step_user(settings)
    assert result["errors"] == {
        "fallback_stt": "unknown_destination",
        "fallback_tts": "unknown_destination",
    }


async def test_options_default_off_and_local_override_audit(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "fallback_stt": "stt.standard",
            "fallback_tts": "tts.standard",
        },
    )
    entry.add_to_hass(hass)
    options = AssistLiveConfigFlow.async_get_options_flow(entry)
    options.hass = hass
    result = await options.async_step_init()
    assert result["data_schema"]({})["record_transcripts"] is False
    result = await options.async_step_init(
        {
            "local_only": True,
            "record_transcripts": True,
            "fallback_stt_local": True,
            "fallback_tts_local": True,
        }
    )
    assert result["type"] == "create_entry"
    assert result["data"]["record_transcripts"] is True
    assert result["data"]["local_decisions"]["stt.standard"]["destination"] == "local"
    assert result["data"]["local_decisions"]["stt.standard"]["recorded_at"].endswith(
        "+00:00"
    )


async def test_cloud_cannot_be_overridden_as_local(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "fallback_stt": "stt.standard",
            "fallback_tts": "tts.standard",
        },
    )
    entry.add_to_hass(hass)
    flow = AssistLiveConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    with patch(
        "custom_components.assist_live.config_flow.entity_iot_class",
        return_value="cloud_push",
    ):
        result = await flow.async_step_init(
            {"local_only": True, "fallback_stt_local": True, "fallback_tts_local": True}
        )
    assert result["errors"]["fallback_stt_local"] == "cloud_destination"


async def test_speaker_after_final_keeps_transcript_bytes(hass, client, ledger, caplog):
    import asyncio

    from assist_live_client.models import HealthCapabilities
    from test_stt_forward import Fallback, audio, entity, metadata

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    events = []
    release = asyncio.Event()
    received = asyncio.Event()

    def speaker(data):
        events.append(data)
        received.set()

    ledger.speaker_event = speaker
    original = "  synthetic command é\n"

    async def messages(turn_id, stream):
        yield {"type": "end_decision"}
        yield {"type": "final", "text": original}
        await release.wait()
        yield {
            "type": "speaker_result",
            "name": "synthetic-speaker",
            "distance": 0.25,
            "destination": "cloud",
        }

    client.stt = messages
    result = await asyncio.wait_for(
        entity(client, ledger, Fallback()).async_process_audio_stream(
            metadata(), audio([bytes(320)])
        ),
        0.1,
    )
    assert result.text.encode() == original.encode()
    assert events == []
    release.set()
    await asyncio.wait_for(received.wait(), 1)
    assert events[0]["name"] == "synthetic-speaker"
    assert events[0]["distance"] == 0.25
    assert events[0]["destination"] == "cloud"
    assert "synthetic-speaker" not in caplog.text
    assert ledger.latest["satellite-a"].destinations[-1] == {
        "part": "speaker",
        "provider": "speaker_plugin",
        "destination": "cloud",
    }


async def test_tts_fallback_refused_at_use_time(hass, client):
    from homeassistant.components.tts import TTSAudioRequest
    from homeassistant.exceptions import HomeAssistantError
    from test_tts_forward import Fallback, entity, text

    from custom_components.assist_live import Runtime

    entry = MockConfigEntry(
        domain="assist_live", data={"satellites": {}, "local_only": True}
    )
    runtime = Runtime(hass, entry, client)
    fallback = Fallback()
    fallback.entity_id = "tts.standard"
    forward = entity(client, runtime.ledger, fallback)
    forward.hass = hass
    failed = []
    hass.bus.async_listen(
        "assist_live_tts_failed", callback(lambda event: failed.append(event.data))
    )
    with patch(
        "custom_components.assist_live.config_flow.entity_iot_class",
        return_value="cloud_push",
    ):
        with pytest.raises(HomeAssistantError):
            await forward.async_stream_tts_audio(
                TTSAudioRequest("en", {"voice": "voice-2"}, text())
            )
    assert not hasattr(fallback, "text")
    await hass.async_block_till_done()
    assert failed[0]["text"] == "First sentence. Second sentence."
    assert "tts_failed" in runtime.ledger.latest["satellite-a"].stages


async def test_midturn_cloud_tts_is_refused_with_full_text(hass, client):
    from assist_live_client.models import (
        HealthCapabilities,
        TTSAudio,
        TTSFormat,
        TTSSentenceEnd,
    )
    from assist_live_client.protocol import ProtocolError
    from homeassistant.components.tts import TTSAudioRequest
    from test_tts_forward import Fallback, entity, text

    from custom_components.assist_live import Runtime

    runtime = Runtime(
        hass,
        MockConfigEntry(
            domain="assist_live", data={"satellites": {}, "local_only": True}
        ),
        client,
    )
    fallback = Fallback()
    fallback.entity_id = "tts.standard"
    forward = entity(client, runtime.ledger, fallback)
    forward.hass = hass
    failed = []
    hass.bus.async_listen(
        "assist_live_tts_failed", callback(lambda event: failed.append(event.data))
    )
    client.health_capabilities.return_value = HealthCapabilities(
        True, True, True, True, True, {"stt": "local", "tts": "local"}
    )

    async def broken(turn_id, parts, voice, **kwargs):
        assert len([part async for part in parts]) == 2
        yield TTSFormat("pcm_s16le", 22050, 1, 2)
        yield TTSAudio(bytes(2))
        yield TTSSentenceEnd(0, 16)
        raise ProtocolError("synthetic loss")

    client.tts_stream = broken
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    with patch(
        "custom_components.assist_live.config_flow.entity_iot_class",
        return_value="cloud_push",
    ):
        assert b"".join([chunk async for chunk in response.data_gen])
    await hass.async_block_till_done()
    assert failed[0]["text"] == "First sentence. Second sentence."
    assert not hasattr(fallback, "text")
    assert [
        row["part"] for row in runtime.ledger.latest["satellite-a"].destinations
    ] == ["tts"]


async def test_midturn_tts_fallback_records_both_destinations(hass, client, ledger):
    import struct

    from assist_live_client.models import (
        HealthCapabilities,
        TTSAudio,
        TTSFormat,
        TTSSentenceEnd,
    )
    from assist_live_client.protocol import ProtocolError
    from homeassistant.components.tts import TTSAudioRequest, TTSAudioResponse
    from test_tts_forward import Fallback, entity, text

    class Compatible(Fallback):
        async def async_stream_tts_audio(self, request):
            self.text = "".join([part async for part in request.message_gen])

            async def output():
                yield struct.pack(
                    "<4sI4s4sIHHIIHH4sI",
                    b"RIFF",
                    38,
                    b"WAVE",
                    b"fmt ",
                    16,
                    1,
                    1,
                    22050,
                    44100,
                    2,
                    16,
                    b"data",
                    2,
                ) + bytes(2)

            return TTSAudioResponse("wav", output())

    async def policy(entity_id):
        return "local"

    ledger.fallback_policy = policy
    client.health_capabilities.return_value = HealthCapabilities(True, True, True, True)

    async def broken(turn_id, parts, voice, **kwargs):
        _ = [part async for part in parts]
        yield TTSFormat("pcm_s16le", 22050, 1, 2)
        yield TTSAudio(bytes(2))
        yield TTSSentenceEnd(0, 16)
        raise ProtocolError("synthetic loss")

    client.tts_stream = broken
    fallback = Compatible()
    fallback.entity_id = "tts.standard"
    forward = entity(client, ledger, fallback)
    forward.hass = hass
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "voice-2"}, text())
    )
    assert b"".join([part async for part in response.data_gen])
    assert [row["part"] for row in ledger.latest["satellite-a"].destinations] == [
        "tts",
        "tts_fallback",
    ]
    assert ledger.latest["satellite-a"].destinations[-1]["destination"] == "local"
    assert fallback.text == "Second sentence."


@pytest.mark.parametrize("ending", ["missing", "unload", "invalid"])
async def test_optional_speaker_read_is_bounded_and_cancelled(client, ledger, ending):
    import asyncio

    from assist_live_client.models import HealthCapabilities
    from test_stt_forward import Fallback, audio, entity, metadata

    entered, closed = asyncio.Event(), asyncio.Event()

    async def stream(*args):
        try:
            yield {"type": "final", "text": "synthetic command"}
            entered.set()
            if ending == "missing":
                return
            if ending == "invalid":
                yield {
                    "type": "speaker_result",
                    "name": "",
                    "distance": 3,
                    "destination": "local",
                }
                return
            await asyncio.Event().wait()
        finally:
            closed.set()

    client.stt = stream
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    forward = entity(client, ledger, Fallback())
    result = await asyncio.wait_for(
        forward.async_process_audio_stream(metadata(), audio([bytes(320)])), 0.1
    )
    assert result.text == "synthetic command"
    await asyncio.wait_for(entered.wait(), 1)
    if ending == "unload":
        await forward.async_will_remove_from_hass()
    await asyncio.wait_for(closed.wait(), 1)
    await ledger.stop_speaker_reads()
    assert ledger.speaker_missing == (1 if ending in {"missing", "invalid"} else 0)
    assert ledger.latest["satellite-a"].speaker is None


@pytest.mark.parametrize(
    "opt_in,server_policy,want",
    [(None, True, False), (True, True, True), (True, False, False)],
)
async def test_transcript_option_through_pinned_wire_and_sqlite(
    hass, tmp_path, opt_in, server_policy, want, socket_enabled
):
    import json
    import sqlite3
    from contextlib import closing
    from dataclasses import asdict

    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from assist_live_client import AssistLiveClient
    from assist_live_client.models import TimingEvent

    from custom_components.assist_live import Runtime

    with closing(sqlite3.connect(tmp_path / "fake.sqlite")) as db:
        db.execute("CREATE TABLE events (body TEXT)")
        opened = []

        async def open_turn(request):
            value = await request.json()
            opened.append(value)
            data = (
                {"transcript": "synthetic command"}
                if server_policy and value["record_transcripts"]
                else {}
            )
            event = TimingEvent(
                value["turn_id"],
                value["satellite"],
                "server",
                "stt_final",
                1,
                "measured",
                None,
                data=data,
            )
            db.execute(
                "INSERT INTO events VALUES (?)",
                (json.dumps({"version": 1, "event_id": 1, "event": asdict(event)}),),
            )
            db.commit()
            return web.json_response({})

        async def export(request):
            assert request.query["satellite"] == "satellite-a"
            return web.Response(
                text="\n".join(row[0] for row in db.execute("SELECT body FROM events"))
                + "\n"
            )

        app = web.Application()
        app.router.add_post("/v1/turns", open_turn)
        app.router.add_get("/v1/export", export)
        async with (
            TestServer(app) as server,
            AssistLiveClient(str(server.make_url("")), "test-only") as actual,
        ):
            entry = MockConfigEntry(
                domain="assist_live",
                data={"satellites": {}},
                options={} if opt_in is None else {"record_transcripts": opt_in},
            )
            ledger = Runtime(hass, entry, actual).ledger
            assert await ledger.connect(ledger.open_voice("satellite-a"))
            rows = [row async for row in actual.export("satellite-a", 0)]
            assert opened[0]["record_transcripts"] is (opt_in is True)
            assert ("transcript" in rows[0]["event"]["data"]) is want
            assert (
                "synthetic command"
                in db.execute("SELECT body FROM events").fetchone()[0]
            ) is want


async def test_real_client_delivers_final_before_speaker_event(hass, socket_enabled):
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from assist_live_client import AssistLiveClient
    from assist_live_client.protocol import encode_control
    from test_stt_forward import Fallback, audio, entity, metadata

    from custom_components.assist_live import Runtime

    release, received = asyncio.Event(), asyncio.Event()
    seen = []

    async def health(request):
        return web.json_response(
            {"version": 1, "healthy": True, "capabilities": {"stt": True}}
        )

    async def accepted(request):
        return web.json_response({})

    async def stream(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hello = await ws.receive_json()
        turn_id = hello["turn_id"]
        await ws.send_json(
            encode_control(
                "negotiated", turn_id, capabilities=["stt", "speaker_result"]
            )
        )
        await ws.send_json(
            encode_control("final", turn_id, text="  synthetic command é\n")
        )
        await release.wait()
        await ws.send_json(
            encode_control(
                "speaker_result",
                turn_id,
                name="synthetic-speaker",
                distance=0.25,
                destination="local",
            )
        )
        await ws.send_json(encode_control("end", turn_id))
        await ws.close()
        return ws

    async def speaker(event):
        seen.append(event.data)
        received.set()

    hass.bus.async_listen("assist_live_speaker", speaker)
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/v1/turns", accepted)
    app.router.add_post("/v1/event", accepted)
    app.router.add_get("/v1/audio", stream)
    async with (
        TestServer(app) as server,
        AssistLiveClient(str(server.make_url("")), "test-only") as actual,
    ):
        runtime = Runtime(
            hass, MockConfigEntry(domain="assist_live", data={"satellites": {}}), actual
        )
        forward = entity(actual, runtime.ledger, Fallback())
        try:
            result = await asyncio.wait_for(
                forward.async_process_audio_stream(metadata(), audio([bytes(320)])), 1
            )
            assert result.text.encode() == "  synthetic command é\n".encode()
            assert seen == []
            release.set()
            await asyncio.wait_for(received.wait(), 1)
            assert seen[0]["name"] == "synthetic-speaker"
            assert seen[0]["distance"] == 0.25
            assert seen[0]["destination"] == "local"
        finally:
            release.set()
            await forward.async_will_remove_from_hass()


@pytest.mark.parametrize(
    "iot_class,decision,allowed",
    [
        (None, False, False),
        (None, True, True),
        ("cloud_push", True, False),
        ("local_polling", False, True),
    ],
)
async def test_stt_checks_current_fallback_destination(
    hass, client, iot_class, decision, allowed
):
    from homeassistant.components import stt
    from test_stt_forward import Fallback, audio, entity, metadata

    from custom_components.assist_live import Runtime

    entry = MockConfigEntry(
        domain="assist_live",
        data={"satellites": {}, "local_only": True},
        options={
            "local_decisions": {
                "stt.standard": {
                    "destination": "local",
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                }
            }
            if decision
            else {}
        },
    )
    runtime = Runtime(hass, entry, client)
    fallback = Fallback()
    fallback.entity_id = "stt.standard"
    with patch(
        "custom_components.assist_live.config_flow.entity_iot_class",
        return_value=iot_class,
    ):
        result = await entity(
            client, runtime.ledger, fallback
        ).async_process_audio_stream(metadata(), audio([bytes(320)]))
    assert (result.result == stt.SpeechResultState.SUCCESS) is allowed
    assert hasattr(fallback, "audio") is allowed
    assert bool(runtime.ledger.latest["satellite-a"].destinations) is allowed


async def test_midturn_stt_records_provider_and_fallback(client, ledger):
    from assist_live_client.models import HealthCapabilities
    from assist_live_client.protocol import ProtocolError
    from test_stt_forward import Fallback, audio, entity, metadata

    async def lost(turn_id, stream):
        await anext(stream)
        raise ProtocolError("synthetic loss")
        yield

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    client.stt = lost
    result = await entity(client, ledger, Fallback()).async_process_audio_stream(
        metadata(), audio([bytes(320)])
    )
    assert result.text == "test command"
    assert [row["part"] for row in ledger.latest["satellite-a"].destinations] == [
        "stt",
        "stt_fallback",
    ]
    events = [call.args[0] for call in client.event.call_args_list]
    assert next(event for event in events if event.stage == "fallback_used").data == {
        "part": "stt_fallback",
        "provider": "unknown",
        "destination": "unknown",
    }
    decision = next(event for event in events if event.stage == "end_decision")
    assert decision.source == "integration"
    assert decision.quality == "proxy"
    assert decision.data["decision_source"] == "ha_audio_end"


async def test_local_speaker_clears_previous_destination_issue(hass, client):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.assist_live import Runtime

    entry = MockConfigEntry(
        domain="assist_live", data={"satellites": {}, "local_only": True}
    )
    runtime = Runtime(hass, entry, client)
    runtime.speaker_metadata({"destination": "cloud"})
    registry = ir.async_get(hass)
    issue_id = f"{entry.entry_id}_speaker_destination"
    assert registry.async_get_issue("assist_live", issue_id) is not None
    runtime.speaker_metadata({"destination": "local"})
    assert registry.async_get_issue("assist_live", issue_id) is None


async def test_background_read_owns_speaker_result(client, ledger):
    import asyncio

    from assist_live_client.models import HealthCapabilities
    from test_stt_forward import Fallback, audio, entity, metadata

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def messages(*args):
        yield {"type": "speaker_result"}
        yield {"type": "final", "text": "synthetic command"}
        yield {
            "type": "speaker_result",
            "name": "synthetic-speaker",
            "distance": 0.2,
            "destination": "local",
        }

    client.stt = messages
    forward = entity(client, ledger, Fallback())
    try:
        result = await forward.async_process_audio_stream(
            metadata(), audio([bytes(320)])
        )
        assert result.text == "synthetic command"
        await asyncio.gather(*(read.task for read in ledger.speaker_reads.values()))
        assert ledger.latest["satellite-a"].speaker["destination"] == "local"
    finally:
        await forward.async_will_remove_from_hass()


async def test_options_listener_updates_ledger_local_only(hass, client):
    from unittest.mock import AsyncMock

    from assist_live_client.models import HealthCapabilities

    from custom_components.assist_live import Runtime, async_setup_entry

    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "url": "https://server.example",
            "token": "test-only",
            "fallback_stt": "stt.standard",
            "fallback_tts": "tts.standard",
            "satellites": {},
            "local_only": True,
        },
        options={"local_only": False},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.assist_live.AssistLiveClient", return_value=client),
        patch.object(Runtime, "start", new_callable=AsyncMock),
        patch.object(Runtime, "_maintenance", new_callable=AsyncMock),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ),
        patch(
            "custom_components.assist_live.assist_pipeline.async_get_pipelines",
            return_value=[],
        ),
    ):
        assert await async_setup_entry(hass, entry)
    runtime = hass.data["assist_live"][entry.entry_id]
    try:
        turn = runtime.ledger.open_voice("satellite-a")
        health = HealthCapabilities(True, True, True, False, False, {"stt": "local"})
        assert runtime.ledger.server_selected(health, "stt", turn.turn_id)
        hass.config_entries.async_update_entry(entry, options={"local_only": True})
        await hass.async_block_till_done()
        assert not runtime.ledger.server_selected(health, "stt", turn.turn_id)
        assert (
            runtime.ledger.records[turn.turn_id].server_refusals["stt"]
            == "server_not_local_only"
        )
        hass.config_entries.async_update_entry(entry, options={"local_only": False})
        await hass.async_block_till_done()
        assert runtime.ledger.server_selected(health, "stt", turn.turn_id)
    finally:
        await entry.async_unload(hass)
