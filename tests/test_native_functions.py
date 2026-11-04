from pathlib import Path

import pytest
from test_pipeline_contract import pipeline_hass as configured_hass  # noqa: F401


def test_native_contract_text_matches_both_document_paths():
    from tools.native_matrix import CONTRACT

    document = (
        Path(__file__).resolve().parents[1] / "docs/testing/native-functions.md"
    ).read_text()
    for function, (expected, method, observer) in CONTRACT.items():
        line = next(
            line
            for line in document.splitlines()
            if line.startswith(f"| `{function}` |")
        )
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        assert cells[1:3] == [expected, expected]
        assert cells[3] == method
        assert cells[4].casefold() == observer


async def agent_bench(hass, monkeypatch, *, same_device=False):
    from homeassistant.components import conversation
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.setup import async_setup_component
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components
    from custom_components.assist_live.ledger import TurnLedger
    from custom_components.assist_live.sensor import TurnLatencySensor

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(
        custom_components,
        "__path__",
        [str(root / "tests/fixtures/custom_components"), *custom_components.__path__],
    )
    assert await async_setup_component(hass, "intent", {})
    from custom_components.assist_live_test_agent import register_timer
    from custom_components.assist_live_test_agent.conversation import TestAgent

    ledger = TurnLedger(None)
    agents, sensors, devices = [], [], []
    entry = MockConfigEntry(domain="assist_live", state=ConfigEntryState.LOADED)
    entry.add_to_hass(hass)
    for index in range(2):
        if same_device and devices:
            entry = MockConfigEntry(domain="assist_live", state=ConfigEntryState.LOADED)
            entry.add_to_hass(hass)
        device = (
            devices[0]
            if same_device and devices
            else dr.async_get(hass).async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={("test", f"virtual-{index}")},
            )
        )
        registered = er.async_get(hass).async_get_or_create(
            "sensor",
            "assist_live",
            f"virtual-{index}_last_turn_latency",
            config_entry=entry,
            device_id=device.id,
        )
        hass.config_entries.async_update_entry(
            entry,
            data={
                "satellites": {
                    **entry.data.get("satellites", {}),
                    device.id: f"virtual-{index}",
                }
            },
        )
        sensor = TurnLatencySensor(f"virtual-{index}", device.id, ledger)
        sensor.hass = hass
        sensor.entity_id = registered.entity_id
        await sensor.async_added_to_hass()
        agent = TestAgent("short" if index == 0 else "long", device.id)
        await hass.data[conversation.DATA_COMPONENT].async_add_entities([agent])
        register_timer(hass, device.id)
        agents.append(agent)
        sensors.append(sensor)
        devices.append(device)
    return ledger, agents, sensors, devices


async def test_two_simulated_satellites_keep_concurrent_turns_and_operations_separate(
    configured_hass,  # noqa: F811
    monkeypatch,
):
    import asyncio

    from homeassistant.components import assist_pipeline, conversation, stt
    from homeassistant.components.assist_pipeline import PipelineStage
    from homeassistant.core import Context, callback

    hass = configured_hass
    ledger, agents, _, devices = await agent_bench(hass, monkeypatch)
    records = []
    hass.bus.async_listen(
        "assist_live_test_record", callback(lambda event: records.append(event.data))
    )
    contexts = [Context(), Context()]
    turns = {}
    original = conversation.async_converse
    arrived = 0
    overlap = asyncio.Event()

    async def synchronized(*args, **kwargs):
        nonlocal arrived
        if kwargs.get("agent_id") == conversation.HOME_ASSISTANT_AGENT:
            arrived += 1
            if arrived == 2:
                overlap.set()
            await asyncio.wait_for(overlap.wait(), 5)
        return await original(*args, **kwargs)

    monkeypatch.setattr(conversation, "async_converse", synchronized)
    pipelines = []
    events = [[], []]

    def processor(index):
        async def process(metadata, stream):
            turns[index] = ledger.open_voice(f"virtual-{index}")
            return stt.SpeechResult(
                f"start a timer for {index + 1} minutes", stt.SpeechResultState.SUCCESS
            )

        return process

    for index, agent in enumerate(agents):
        engine_id = "stt.contract_stt" if index == 0 else "stt.alternate_stt"
        engine = stt.async_get_speech_to_text_entity(hass, engine_id)
        monkeypatch.setattr(engine, "async_process_audio_stream", processor(index))
        pipeline = await assist_pipeline.async_create_default_pipeline(
            hass, engine_id, "tts.contract_tts", f"Simulated satellite {index}"
        )
        assert pipeline is not None
        await assist_pipeline.async_update_pipeline(
            hass,
            pipeline,
            conversation_engine=agent.entity_id,
            prefer_local_intents=False,
        )
        pipelines.append(pipeline)

    async def audio():
        yield bytes(640)

    await asyncio.gather(
        *[
            assist_pipeline.async_pipeline_from_audio_stream(
                hass,
                context=contexts[index],
                event_callback=events[index].append,
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
                device_id=devices[index].id,
                satellite_id=f"assist_satellite.simulated_{index}",
                end_stage=PipelineStage.INTENT,
            )
            for index, pipeline in enumerate(pipelines)
        ]
    )
    await hass.async_block_till_done()
    assert arrived == len(records) == 2
    for index, device in enumerate(devices):
        record = next(record for record in records if record["device_id"] == device.id)
        assert record["turn_id"] == turns[index].turn_id
        assert record["context_id"] == contexts[index].id
        assert record["completed"] is True
        assert len(record["operations"]) == 1
        assert record["operations"][0]["arguments"] == {"seconds": (index + 1) * 60}
        assert [event.type for event in events[index]][-1] == "run-end"
        assert "intent-end" in [event.type for event in events[index]]
        assert "error" not in [event.type for event in events[index]]


@pytest.mark.parametrize("case", ["stale", "overlap", "claimed"])
async def test_stale_or_overlapping_voice_turn_never_links_a_later_conversation(
    configured_hass,  # noqa: F811
    monkeypatch,
    case,
):
    from homeassistant.components import conversation
    from homeassistant.core import Context, callback
    from homeassistant.util.ulid import ulid_at_time

    hass = configured_hass
    ledger, agents, sensors, devices = await agent_bench(
        hass, monkeypatch, same_device=True
    )
    records = []
    hass.bus.async_listen(
        "assist_live_test_record", callback(lambda event: records.append(event.data))
    )
    turn = ledger.open_voice("virtual-0")
    context = Context()
    if case == "stale":
        state = hass.states.get(sensors[0].entity_id)
        context = Context(id=ulid_at_time(state.last_updated.timestamp() + 1))
    elif case == "overlap":
        ledger.open_voice("virtual-1")
    else:
        hass.data["assist_live_test_turn_claims"] = {turn.turn_id: "previous-context"}
    await conversation.async_converse(
        hass,
        "start a timer for five minutes",
        None,
        context,
        language="en",
        agent_id=agents[0].entity_id,
        device_id=devices[0].id,
    )
    await hass.async_block_till_done()
    assert len(records) == 1
    assert records[0]["turn_id"] is None
