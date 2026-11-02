import asyncio
from dataclasses import asdict
from unittest.mock import patch

import pytest
from assist_live_client import AssistLiveClient
from assist_live_client.models import (
    HealthCapabilities,
    TimingEvent,
    TTSAudio,
    TTSFormat,
)
from assist_live_client.protocol import ProtocolError
from homeassistant.components.tts import TTSAudioRequest
from test_stt_forward import Fallback as STTFallback
from test_stt_forward import audio, metadata
from test_stt_forward import entity as stt_entity
from test_tts_forward import Fallback as TTSFallback
from test_tts_forward import entity as tts_entity
from test_tts_forward import text


@pytest.mark.parametrize(
    "name,parameters",
    [
        ("AssistLiveClient", "url token session"),
        ("AssistLiveClient.bindings", "self"),
        ("AssistLiveClient.close", "self"),
        ("AssistLiveClient.health_capabilities", "self timeout"),
        ("AssistLiveClient.open_turn", "self turn"),
        ("AssistLiveClient.event", "self event"),
        ("AssistLiveClient.events", "self satellite after_event_id"),
        ("AssistLiveClient.stt", "self turn_id audio"),
        ("AssistLiveClient.tts_stream", "self turn_id text voice text_positions"),
        (
            "HealthCapabilities",
            "healthy stt tts text_positions local_only destinations "
            "tts_voices tts_default_voice",
        ),
        (
            "TimingEvent",
            "turn_id satellite source stage t_utc_ns quality uncertainty_ms "
            "t_device_ms seq data",
        ),
        (
            "TurnOpen",
            "turn_id satellite turn_type parent_turn_id conversation_id "
            "record_transcripts opened_utc_ns",
        ),
        ("TTSFormat", "codec rate channels width"),
        ("TTSAudio", "data"),
        ("TTSSentenceEnd", "sentence text_end"),
    ],
)
def test_fifteen_public_client_api_signatures(name, parameters):
    import inspect

    from assist_live_client import models

    if name == "AssistLiveClient":
        target = AssistLiveClient
    elif name.startswith("AssistLiveClient."):
        target = getattr(AssistLiveClient, name.split(".")[1])
    else:
        target = getattr(models, name)
    assert list(inspect.signature(target).parameters) == parameters.split()


async def run_entities(client, ledger, *, fallback):
    client.health_capabilities.return_value = HealthCapabilities(
        not fallback, True, True
    )

    async def recognize(turn_id, stream):
        async for _ in stream:
            pass
        yield {"type": "end_decision"}
        yield {"type": "final", "text": "test command"}

    async def synthesize(turn_id, parts, voice, **kwargs):
        async for _ in parts:
            pass
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x00\x00")

    client.stt = recognize
    client.tts_stream = synthesize
    forward = stt_entity(client, ledger, STTFallback())
    try:
        result = await forward.async_process_audio_stream(
            metadata(), audio([bytes(320)])
        )
        assert result.text == "test command"
        turn = ledger.current("satellite-a")
        response = await tts_entity(
            client, ledger, TTSFallback()
        ).async_stream_tts_audio(TTSAudioRequest("en", {"voice": "voice-2"}, text()))
        assert b"".join([chunk async for chunk in response.data_gen])
        assert ledger.current("satellite-a") == turn, "receipt must retain voice turn"
        return turn
    finally:
        await forward.async_will_remove_from_hass()


@pytest.mark.parametrize("fallback", [False, True], ids=["server", "fallback"])
async def test_entity_events_obey_measurement_owners(client, ledger, fallback):
    turn = await run_entities(client, ledger, fallback=fallback)
    events = [call.args[0] for call in client.event.call_args_list] + ledger._pending
    stages = [event.stage for event in events]
    if fallback:
        assert "end_decision" in stages
        assert "stt_final" in stages
        assert "tts_first_byte" in stages
        end = next(event for event in events if event.stage == "end_decision")
        assert end.quality == "proxy"
        assert end.data == {"decision_source": "ha_audio_end"}
        uses = [event.data for event in events if event.stage == "fallback_used"]
        assert uses == [
            {"reason": "server_unhealthy", "part": "stt"},
            {"reason": "server_unhealthy", "part": "tts"},
        ], "fallback codes must name the affected part only"
    else:
        assert "stt_final_received" in stages, "HA final receipt is missing"
        assert "tts_audio_received" in stages, "HA audio receipt is missing"
        assert "stt_final" not in stages, "server final has a second owner"
        assert "end_decision" not in stages, "server decision has a second owner"
        assert "tts_first_byte" not in stages, "server byte has a second owner"
    assert "tts_first_text" in stages
    assert all(event.turn_id == turn.turn_id for event in events)
    assert turn.conversation_id is None
    await ledger.stop_events()


@pytest.mark.parametrize("fallback", [False, True], ids=["server", "fallback"])
@pytest.mark.parametrize(
    "stages,server_owner,fallback_owner",
    [
        (
            "speech_start end_candidate end_decision stt_partial stt_final",
            "server",
            "integration",
        ),
        ("tts_first_text", "integration", "integration"),
        ("tts_first_byte", "server", "integration"),
        ("tts_sentence tts_last_byte", "server", None),
        ("fallback_used turn_superseded", "integration", "integration"),
        ("satellite_state", "ha_state", "ha_state"),
        ("stt_failed tts_failed", "server", "integration"),
        ("speaker_result tts_text_received unmatched", "server", "server"),
        (
            "stt_final_received tts_audio_received late_result_queued "
            "late_result_delivering late_result_spoken late_result_cancelled "
            "late_result_uncertain late_result_notify_sent "
            "late_result_notify_failed late_result_expired",
            "integration",
            "integration",
        ),
    ],
)
async def test_owner_table_on_both_entity_paths(
    client, ledger, fallback, stages, server_owner, fallback_owner
):
    turn = await run_entities(client, ledger, fallback=fallback)
    ledger.satellite_state("satellite-a", "responding")
    ledger.open_voice("satellite-a")
    events = [call.args[0] for call in client.event.call_args_list] + ledger._pending
    owner = fallback_owner if fallback else server_owner
    for event in events:
        if event.stage in stages.split():
            assert event.source == owner, f"wrong owner for {event.stage}"
    assert all(event.turn_id is not None for event in events)
    assert turn.conversation_id is None
    await ledger.stop_events()


async def test_no_active_turn_does_not_send_unbound_timing_events(client, ledger):
    ledger.satellite_state("satellite-a", "idle")
    ledger.mark(None, "tts_first_text")
    ledger.mark("unknown", "tts_first_text")
    ledger.schedule_events(retry=True)
    assert not ledger._pending
    client.event.assert_not_awaited()


async def test_audio_receipt_is_recorded_before_ha_consumes_output(client, ledger):
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)

    async def synthesize(turn_id, parts, voice, **kwargs):
        async for _ in parts:
            pass
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x00\x00")

    client.tts_stream = synthesize
    response = await tts_entity(client, ledger, TTSFallback()).async_stream_tts_audio(
        TTSAudioRequest("en", {}, text())
    )
    try:
        assert "tts_audio_received" in ledger.latest["satellite-a"].stages, (
            "audio receipt waits for HA output consumption"
        )
    finally:
        assert b"".join([chunk async for chunk in response.data_gen])
        await ledger.stop_events()


@pytest.mark.parametrize("part", ["stt", "tts"])
@pytest.mark.parametrize("destination", ["none", "server", "fallback"])
@pytest.mark.parametrize("fallback", ["none", "same_part", "other_part"])
async def test_failure_stage_has_only_its_path_owner(
    client, ledger, part, destination, fallback
):
    from assist_live_client.models import STAGE_OWNERS

    stored = []

    async def accept(event):
        event_part = "tts" if event.stage.startswith("tts_") else "stt"
        has_fallback = any(
            row.stage == "fallback_used" and row.data.get("part") == event_part
            for row in stored
        )
        owner = STAGE_OWNERS[event.stage]["fallback" if has_fallback else "server"]
        assert event.source == owner, "failure sent without stored same-part fallback"
        stored.append(event)

    client.event.side_effect = accept
    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    if fallback != "none":
        fallback_part = (
            part if fallback == "same_part" else ("tts" if part == "stt" else "stt")
        )
        await ledger.use_fallback(
            turn.turn_id,
            f"{fallback_part}_fallback",
            "test-engine",
            reason="stream_closed",
        )
        await ledger.flush()
    ledger.records[turn.turn_id].destinations.clear()
    if destination != "none":
        ledger.used(
            turn.turn_id,
            part if destination == "server" else f"{part}_fallback",
            "test-engine",
            "local",
        )
    ledger.mark(turn.turn_id, f"{part}_failed")
    await ledger.flush()
    failures = [event for event in stored if event.stage == f"{part}_failed"]
    assert len(failures) == int(fallback == "same_part"), (
        "fallback failure missing despite stored same-part fallback"
    )
    assert ledger.failures[f"{part}_failed"] == 1
    assert ledger.timing_events_dropped == 0
    if failures:
        assert failures[0].source == "integration"
    await ledger.stop_events()


@pytest.mark.parametrize("part", ["stt", "tts"])
@pytest.mark.parametrize("marker_accepted", [False, True])
async def test_failure_waits_for_accepted_fallback_marker(
    client, ledger, part, marker_accepted
):
    stored = []

    async def accept(event):
        if event.stage == "fallback_used" and not marker_accepted:
            raise ProtocolError("marker refused", kind="refused")
        if event.stage == f"{part}_failed":
            assert any(
                row.stage == "fallback_used" and row.data.get("part") == part
                for row in stored
            ), "failure sent after fallback marker refusal"
        stored.append(event)

    client.event.side_effect = accept
    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    await ledger.use_fallback(
        turn.turn_id, f"{part}_fallback", "test-engine", reason="stream_closed"
    )
    ledger.mark(turn.turn_id, f"{part}_failed")
    await ledger.flush()
    assert sum(row.stage == f"{part}_failed" for row in stored) == int(marker_accepted)
    assert ledger.failures[f"{part}_failed"] == 1
    assert ledger.timing_events_dropped == int(not marker_accepted)
    await ledger.stop_events()


@pytest.mark.parametrize(
    "reason",
    [
        "wrong_owner",
        "turn_event_limit",
        "turn_size_limit",
        "turn_required",
        "invalid_conversation_id",
    ],
)
async def test_refused_event_is_removed_counted_and_next_is_sent(
    client, ledger, reason, caplog
):
    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    ledger.mark(turn.turn_id, "tts_first_text")
    ledger.mark(turn.turn_id, "turn_superseded")
    client.event.side_effect = [ProtocolError(reason, kind="refused"), None]
    await ledger.flush()
    assert client.event.await_count == 2, "refusal stopped later good event"
    assert not ledger._pending, "refused event was retained"
    assert ledger.timing_events_dropped == 1, "refused event removal was not counted"
    assert reason not in caplog.text
    assert turn.turn_id not in caplog.text


@pytest.mark.parametrize(
    "error", [TimeoutError(), OSError(), ProtocolError("lost", kind="closed")]
)
async def test_transient_event_error_retains_event_until_retry(client, ledger, error):
    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    ledger.mark(turn.turn_id, "tts_first_text")
    client.event.side_effect = error
    await ledger.flush()
    assert len(ledger._pending) == 1
    assert ledger.timing_events_dropped == 0
    client.event.side_effect = None
    ledger.schedule_events(retry=True)
    await asyncio.wait_for(ledger._event_sender, 1)
    assert not ledger._pending


async def test_inflight_refusal_counts_one_removal_after_overflow(client, ledger):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def send(event):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            raise ProtocolError("wrong_owner", kind="refused")

    client.event.side_effect = send
    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    ledger.mark(turn.turn_id, "tts_first_text")
    ledger.schedule_events()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(512):
            ledger.mark(turn.turn_id, "tts_first_text")
        release.set()
        await asyncio.wait_for(ledger._event_sender, 1)
        assert ledger.timing_events_dropped == 1, "one removed event was counted twice"
        assert not ledger._pending
    finally:
        release.set()
        await ledger.stop_events()


async def test_connect_during_late_open_does_not_open_same_turn_twice(client, ledger):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def opened(turn):
        nonlocal calls
        calls += 1
        assert calls == 1, "same turn opened twice during recovery"
        entered.set()
        await release.wait()

    client.open_turn.side_effect = opened
    turn = ledger.open_voice("satellite-a")
    ledger.schedule_events(retry=True)
    await asyncio.wait_for(entered.wait(), 1)
    connected = asyncio.create_task(ledger.connect(turn))
    asyncio.get_running_loop().call_soon(release.set)
    try:
        assert await asyncio.wait_for(connected, 1)
        await asyncio.wait_for(ledger._event_sender, 1)
        assert calls == 1
    finally:
        release.set()
        await ledger.stop_events()


async def test_late_open_precedes_events_and_keeps_initial_time(client, ledger):
    with patch(
        "custom_components.assist_live.ledger.time.time_ns", return_value=123456789
    ):
        turn = ledger.open_voice("satellite-a")
    client.open_turn.side_effect = OSError()
    assert not await ledger.connect(turn)
    ledger.mark(turn.turn_id, "stt_final")
    ledger.device_event("satellite-a", turn.turn_id, "playback_end")
    ledger.clock = lambda: ledger.records[turn.turn_id].opened + 200
    ledger.prune()
    assert len(ledger._pending) == 1, "outage pruning lost the turn events"
    calls = []

    async def opened(request):
        assert request.opened_utc_ns == 123456789, "late open lost initial UTC time"
        assert request.conversation_id is None
        calls.append("open")

    async def event(request):
        assert calls == ["open"], "event was sent before the late turn open"
        assert request.turn_id == turn.turn_id
        calls.append("event")

    client.open_turn.side_effect = opened
    client.event.side_effect = event
    ledger.schedule_events(retry=True)
    if ledger._event_sender is not None:
        await asyncio.wait_for(ledger._event_sender, 1)
    assert calls == ["open", "event"], "late open and event were not retried"
    assert not ledger._pending


async def test_fallback_health_outage_retains_open_without_connect_attempt(
    client, ledger
):
    turn = await run_entities(client, ledger, fallback=True)
    client.open_turn.assert_not_awaited()
    ledger.schedule_events(retry=True)
    if ledger._event_sender is not None:
        await asyncio.wait_for(ledger._event_sender, 1)
    assert client.open_turn.await_count == 1, "fallback turn open was not retained"
    assert client.open_turn.call_args.args[0] == turn
    assert not ledger._pending


@pytest.mark.parametrize(
    "turn_exists", [True, False], ids=["accepted-open", "absent-turn"]
)
async def test_refused_late_open_still_sends_each_queued_event(
    aiohttp_server, socket_enabled, turn_exists, hass
):
    from aiohttp import web
    from assist_live_client.models import TurnOpen
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    paths = []
    stages = []

    async def opened(request):
        TurnOpen(**await request.json())
        paths.append("open")
        return web.json_response({}, status=409)

    async def event(request):
        parsed = TimingEvent(**await request.json())
        assert parsed.turn_id is not None
        paths.append("event")
        stages.append(parsed.stage)
        return web.json_response({}, status=200 if turn_exists else 400)

    app = web.Application()
    app.router.add_post("/v1/turns", opened)
    app.router.add_post("/v1/event", event)
    server = await aiohttp_server(app)
    async with AssistLiveClient(str(server.make_url("")), "test-token") as real:
        entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
        runtime = Runtime(hass, entry, real)
        hass.data["assist_live"] = {entry.entry_id: runtime}
        ledger = runtime.ledger
        turn = ledger.open_voice("satellite-a")
        ledger.mark(turn.turn_id, "tts_first_text")
        ledger.mark(turn.turn_id, "turn_superseded")
        ledger.schedule_events(retry=True)
        try:
            await asyncio.wait_for(ledger._event_sender, 1)
            assert paths == ["open", "event", "event"], (
                "refused late open blocked queued events"
            )
            assert stages == ["tts_first_text", "turn_superseded"]
            assert not ledger._pending
            assert ledger.timing_events_dropped == (0 if turn_exists else 2)
            result = await async_get_config_entry_diagnostics(hass, entry)
            assert result.get("turn_opens_dropped", 0) == 1, (
                "refused open removal was not counted separately"
            )
            assert result["timing_events_dropped"] == (0 if turn_exists else 2)
        finally:
            await ledger.stop_events()


async def test_pinned_client_serializes_receipts_and_initial_open_time(
    aiohttp_server, socket_enabled
):
    from aiohttp import web
    from assist_live_client.models import TurnOpen

    from custom_components.assist_live.ledger import TurnLedger

    received = []

    async def accept(request):
        body = await request.json()
        if request.path == "/v1/turns":
            parsed = TurnOpen(**body)
            assert parsed.opened_utc_ns == 987654321, "wire open lost initial UTC time"
            assert parsed.conversation_id is None
        else:
            parsed = TimingEvent(**body)
            assert parsed.stage in {"stt_final_received", "tts_audio_received"}
            assert parsed.source == "integration"
            assert parsed.turn_id is not None, "wire event has no turn_id"
        assert asdict(parsed) == body
        received.append(request.path)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/v1/turns", accept)
    app.router.add_post("/v1/event", accept)
    server = await aiohttp_server(app)
    async with AssistLiveClient(str(server.make_url("")), "test-token") as real:
        ledger = TurnLedger(real)
        with patch(
            "custom_components.assist_live.ledger.time.time_ns", return_value=987654321
        ):
            turn = ledger.open_voice("satellite-a")
        assert await ledger.connect(turn)
        ledger.mark(turn.turn_id, "stt_final_received")
        ledger.mark(turn.turn_id, "tts_audio_received")
        await ledger.flush()
        assert received == ["/v1/turns", "/v1/event", "/v1/event"]
        await ledger.stop_events()
