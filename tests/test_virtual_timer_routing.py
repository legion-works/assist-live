from pathlib import Path
from unittest.mock import patch

import pytest
from test_pipeline_contract import pipeline_hass as configured_hass  # noqa: F401


@pytest.mark.parametrize("group", ["short", "long"])
@pytest.mark.parametrize("input_device", ["satellite", "virtual"])
@pytest.mark.parametrize("delayed", [False, True])
async def test_native_timer_uses_only_virtual_receiver(
    configured_hass,  # noqa: F811
    monkeypatch,
    group,
    input_device,
    delayed,
):
    from homeassistant.components import conversation
    from homeassistant.components.homeassistant.exposed_entities import (
        async_expose_entity,
    )
    from homeassistant.components.intent import async_register_timer_handler
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.core import Context, callback
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import intent
    from homeassistant.setup import async_setup_component
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components
    from custom_components.assist_live.ledger import TurnLedger
    from custom_components.assist_live.sensor import TurnLatencySensor

    hass = configured_hass
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(
        custom_components,
        "__path__",
        [str(root / "tests/fixtures/custom_components"), *custom_components.__path__],
    )
    assert await async_setup_component(hass, "intent", {})
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"test_light": {"name": "Test light"}}}
    )
    async_expose_entity(hass, "conversation", "input_boolean.test_light", True)
    from custom_components.assist_live_test_agent.conversation import (
        REPLIES,
        TestAgent,
    )

    from custom_components import assist_live_test_agent as fixture

    entry = MockConfigEntry(domain="assist_live_test_agent")
    entry.add_to_hass(hass)
    devices = {}
    for name in ("satellite", "virtual"):
        area = ar.async_get(hass).async_create(f"{name.title()} test area")
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("test", name)}
        )
        devices[name] = dr.async_get(hass).async_update_device(
            device.id, area_id=area.id
        )
    satellite = er.async_get(hass).async_get_or_create(
        "assist_satellite", "test", "satellite", device_id=devices["satellite"].id
    )
    satellite_events, virtual_events, records = [], [], []
    remove_satellite = async_register_timer_handler(
        hass,
        devices["satellite"].id,
        callback(lambda event, timer: satellite_events.append((event, timer))),
    )

    def observe_receiver(hass, device_id, handler):
        @callback
        def receive(event, timer):
            virtual_events.append((event, timer))
            handler(event, timer)

        return async_register_timer_handler(hass, device_id, receive)

    monkeypatch.setattr(fixture, "async_register_timer_handler", observe_receiver)
    remove_virtual = fixture.register_timer(hass, devices["virtual"].id)
    agent = TestAgent(group, devices["virtual"].id)
    await hass.data[conversation.DATA_COMPONENT].async_add_entities([agent])
    hass.bus.async_listen(
        "assist_live_test_record", callback(lambda event: records.append(event.data))
    )
    live_entry = MockConfigEntry(
        domain="assist_live",
        state=ConfigEntryState.LOADED,
        data={"satellites": {devices[input_device].id: "input"}},
    )
    live_entry.add_to_hass(hass)
    registered = er.async_get(hass).async_get_or_create(
        "sensor",
        "assist_live",
        "input_last_turn_latency",
        config_entry=live_entry,
        device_id=devices[input_device].id,
    )
    ledger = TurnLedger(None)
    sensor = TurnLatencySensor("input", devices[input_device].id, ledger)
    sensor.hass = hass
    sensor.entity_id = registered.entity_id
    await sensor.async_added_to_hass()
    context = Context()
    turn = ledger.open_voice("input")
    command = (
        "turn off test light in five minutes"
        if delayed
        else "start a timer for five minutes"
    )
    try:
        with patch.object(intent, "async_handle", wraps=intent.async_handle) as native:
            result = await conversation.async_converse(
                hass,
                command,
                None,
                context,
                language="en",
                agent_id=agent.entity_id,
                device_id=devices[input_device].id,
                satellite_id=satellite.entity_id
                if input_device == "satellite"
                else None,
            )
        await hass.async_block_till_done()
        assert result.response.response_type.value == "action_done"
        assert result.response.speech["plain"]["speech"] == REPLIES[group]
        assert len(records) == 1
        record = records[0]
        assert record["device_id"] == devices[input_device].id, "input device record"
        assert record["turn_id"] == turn.turn_id, "input device operation bridge"
        assert record["turn_type"] == "voice"
        assert record["context_id"] == context.id
        assert record["completed"] is True
        assert record["selected_intent"]["name"] == "HassStartTimer"
        assert native.call_args.kwargs["device_id"] == devices["virtual"].id, (
            "native timer device"
        )
        assert native.call_args.kwargs["satellite_id"] is None
        assert record["selected_intent"]["slots"]["preferred_area_id"] == (
            devices["virtual"].area_id
        )
        assert satellite_events == [], "satellite received a timer event"
        assert len(record["operations"]) == 1, "timer operation recorded"
        operation = record["operations"][0]
        assert operation["service"] == (
            "timer.scheduled" if delayed else "timer.started"
        )
        assert operation["target"] == fixture.DEVICE_ID
        assert operation["arguments"] == (
            {"seconds": 300, "command": "turn off test light"}
            if delayed
            else {"seconds": 300}
        )
        assert operation["t_monotonic_ns"] > 0
        if delayed:
            assert virtual_events == []
            assert record["actions"][0]["kind"] == "delayed_action"
        else:
            assert len(virtual_events) == 1, "virtual receiver got the timer"
            event, timer = virtual_events[0]
            assert event.value == "started"
            assert timer.device_id == devices["virtual"].id
            assert timer.seconds == 300
            assert record["actions"] == [
                {"kind": "timer", "event": "started", "seconds": 300}
            ]
        assert record["text_deltas"] == (
            [REPLIES[group][:45], REPLIES[group][45:]] if group == "long" else []
        )
    finally:
        remove_satellite()
        remove_virtual()


@pytest.mark.parametrize("group", ["short", "long"])
async def test_input_satellite_pipeline_keeps_the_spoken_reply(
    configured_hass,  # noqa: F811
    monkeypatch,
    group,
):
    import io
    import wave

    from homeassistant.components import assist_pipeline, stt, tts
    from homeassistant.components.tts.helper import get_engine_instance
    from homeassistant.core import Context
    from homeassistant.helpers import entity_registry as er
    from test_native_functions import agent_bench

    hass = configured_hass
    _, agents, _, devices = await agent_bench(hass, monkeypatch)
    from custom_components.assist_live_test_agent.conversation import REPLIES

    index = 0 if group == "short" else 1
    satellite = er.async_get(hass).async_get_or_create(
        "assist_satellite", "test", "reply", device_id=devices[1 - index].id
    )
    wav = io.BytesIO()
    with wave.open(wav, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(bytes(640))
    speech_audio = wav.getvalue()

    async def recognize(metadata, stream):
        return stt.SpeechResult(
            "start a timer for five minutes", stt.SpeechResultState.SUCCESS
        )

    async def synthesize(message, language, options=None):
        return "wav", speech_audio

    engine = stt.async_get_speech_to_text_entity(hass, "stt.contract_stt")
    monkeypatch.setattr(engine, "async_process_audio_stream", recognize)
    engine = get_engine_instance(hass, "tts.contract_tts")
    monkeypatch.setattr(engine, "async_get_tts_audio", synthesize)
    pipeline = await assist_pipeline.async_create_default_pipeline(
        hass, "stt.contract_stt", "tts.contract_tts", "Satellite reply"
    )
    assert pipeline is not None
    await assist_pipeline.async_update_pipeline(
        hass,
        pipeline,
        conversation_engine=agents[index].entity_id,
        prefer_local_intents=False,
    )

    async def audio():
        yield bytes(640)

    events = []
    await assist_pipeline.async_pipeline_from_audio_stream(
        hass,
        context=Context(),
        event_callback=events.append,
        stt_metadata=stt.SpeechMetadata(
            language="en",
            format=stt.AudioFormats.WAV,
            codec=stt.AudioCodecs.PCM,
            bit_rate=stt.AudioBitRates.BITRATE_16,
            sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
            channel=stt.AudioChannels.CHANNEL_MONO,
        ),
        stt_stream=audio(),
        pipeline_id=pipeline.id,
        device_id=devices[1 - index].id,
        satellite_id=satellite.entity_id,
        tts_audio_output={"preferred_format": "wav"},
    )
    tts_end = next(event for event in events if event.type == "tts-end")
    media_id = tts_end.data["tts_output"]["media_id"].removeprefix(
        "media-source://tts/"
    )
    assert await tts.async_get_media_source_audio(hass, media_id) == (
        "wav",
        speech_audio,
    )
    await hass.async_block_till_done()
    tts_start = next(event for event in events if event.type == "tts-start")
    assert tts_start.data["tts_input"] == REPLIES[group], (
        "input satellite reply reaches TTS"
    )
    assert "tts-end" in [event.type for event in events]
    assert events[-1].type == "run-end"
    assert "error" not in [event.type for event in events]
