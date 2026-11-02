import asyncio
from unittest.mock import patch

import pytest
from homeassistant.components import stt
from homeassistant.components.tts import TTSAudioRequest, TTSAudioResponse, Voice
from homeassistant.components.tts.const import DATA_COMPONENT as TTS_COMPONENT
from homeassistant.data_entry_flow import InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_pipeline_contract import pipeline_hass  # noqa: F401
from test_stt_forward import audio, metadata


@pytest.fixture
async def fallback_entry(pipeline_hass, client, enable_custom_integrations):  # noqa: F811
    hass = pipeline_hass

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "url": "https://server.example",
            "token": "test-only",
            "fallback_stt": "stt.contract_stt",
            "fallback_tts": "tts.contract_tts",
            "fallback_tts_voice": "voice-one",
            "satellites": {"device-a": "satellite-a"},
            "pipeline_selects": {},
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        yield hass, entry
        assert await hass.config_entries.async_unload(entry.entry_id)


def values(**changes):
    return {
        "fallback_stt": "stt.contract_stt",
        "fallback_tts": "tts.contract_tts",
        "fallback_tts_voice": "voice-one",
        "local_only": False,
        "record_transcripts": False,
        "fallback_stt_local": False,
        "fallback_tts_local": False,
        **changes,
    }


async def start(hass, entry):
    return await hass.config_entries.options.async_init(entry.entry_id)


async def submit(hass, form, data):
    return await hass.config_entries.options.async_configure(form["flow_id"], data)


def voice_options(form):
    schema = form["data_schema"].schema
    field = next(key for key in schema if str(key) == "fallback_tts_voice")
    return schema[field].config["options"]


async def test_options_change_stt_next_turn_uses_selected_entity(fallback_entry):
    hass, entry = fallback_entry
    old_runtime = hass.data["assist_live"][entry.entry_id]
    form = await start(hass, entry)
    defaults = form["data_schema"]({})
    assert defaults["fallback_stt"] == "stt.contract_stt", "current STT selector"
    assert defaults["fallback_tts"] == "tts.contract_tts", "current TTS selector"
    result = await submit(hass, form, values(fallback_stt="stt.alternate_stt"))
    assert result["type"] == "create_entry", "STT change stays one step"
    await hass.async_block_till_done()
    runtime = hass.data["assist_live"][entry.entry_id]
    assert runtime is not old_runtime, "options update reloads the entry"
    assert entry.options["fallback_stt"] == "stt.alternate_stt", "STT saved in options"
    selected = stt.async_get_speech_to_text_entity(hass, "stt.alternate_stt")
    original = stt.async_get_speech_to_text_entity(hass, "stt.contract_stt")
    seen = []

    async def transcribe(meta, stream):
        seen.append(b"".join([chunk async for chunk in stream]))
        return stt.SpeechResult("new fallback", stt.SpeechResultState.SUCCESS)

    with (
        patch.object(selected, "async_process_audio_stream", side_effect=transcribe),
        patch.object(
            original,
            "async_process_audio_stream",
            return_value=stt.SpeechResult(
                "old fallback", stt.SpeechResultState.SUCCESS
            ),
        ),
    ):
        result = await runtime.stt["device-a"].async_process_audio_stream(
            metadata(), audio([bytes(320)])
        )
    assert result.text == "new fallback", "next STT turn uses selected fallback"
    assert seen == [bytes(320)], "new STT receives next turn audio"
    reopened = await start(hass, entry)
    assert reopened["data_schema"]({})["fallback_stt"] == "stt.alternate_stt"


async def test_options_tts_change_lists_new_voices_and_revalidates(fallback_entry):
    hass, entry = fallback_entry
    engine = hass.data[TTS_COMPONENT].get_entity("tts.alternate_tts")
    voices = [Voice("new-voice", "New")]
    with patch.object(engine, "async_get_supported_voices", return_value=voices):
        form = await start(hass, entry)
        form = await submit(hass, form, values(fallback_tts="tts.alternate_tts"))
        assert form.get("step_id") == "fallback_voice", "TTS change needs voice step"
        assert entry.options == {}, "engine change is not saved before voice choice"
        assert voice_options(form) == [
            {"value": "", "label": "Default"},
            {"value": "new-voice", "label": "New"},
        ], "new engine voices replace old voices"
        with pytest.raises(InvalidData, match="fallback_tts_voice"):
            await submit(hass, form, {"fallback_tts_voice": "voice-one"})
        voices[:] = [Voice("replacement", "Replacement")]
        form = await submit(hass, form, {"fallback_tts_voice": "new-voice"})
        assert form.get("errors") == {"fallback_tts_voice": "invalid_fallback_voice"}, (
            "voice is revalidated against the new engine before save"
        )
        assert entry.options == {}, "invalid voice cannot save options"
        result = await submit(hass, form, {"fallback_tts_voice": "replacement"})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    forward = hass.data["assist_live"][entry.entry_id].tts["device-a"]

    async def reply():
        yield "Synthetic reply."

    async def synth(request):
        assert request.options["voice"] == "replacement", (
            "new fallback receives new voice"
        )
        assert (
            "".join([part async for part in request.message_gen]) == "Synthetic reply."
        )

        async def output():
            yield b"new fallback audio"

        return TTSAudioResponse("wav", output())

    with patch.object(engine, "async_stream_tts_audio", side_effect=synth):
        response = await forward.async_stream_tts_audio(
            TTSAudioRequest("en", {}, reply())
        )
        assert (
            b"".join([chunk async for chunk in response.data_gen])
            == b"new fallback audio"
        ), "next TTS turn uses selected fallback"
    assert entry.options["fallback_tts"] == "tts.alternate_tts"
    assert entry.options["fallback_tts_voice"] == "replacement"


@pytest.mark.parametrize(
    "field,part", [("fallback_stt", "stt"), ("fallback_tts", "tts")]
)
async def test_options_refuse_assist_live_entity(fallback_entry, field, part):
    hass, entry = fallback_entry
    runtime = hass.data["assist_live"][entry.entry_id]
    entity_id = getattr(runtime, part)["device-a"].entity_id
    form = await start(hass, entry)
    result = await submit(hass, form, values(**{field: entity_id}))
    assert result.get("errors", {}).get(field) == "fallback_loop", (
        "assist-live fallback is refused in each options field"
    )
    assert entry.options == {}


@pytest.mark.parametrize(
    "field,entity_id",
    [("fallback_stt", "stt.missing"), ("fallback_tts", "tts.missing")],
)
async def test_options_refuse_missing_entity(fallback_entry, field, entity_id):
    hass, entry = fallback_entry
    form = await start(hass, entry)
    result = await submit(hass, form, values(**{field: entity_id}))
    assert result.get("errors", {}).get(field) == "invalid_fallback_entity", (
        "missing fallback entity cannot be saved"
    )


async def test_options_local_only_checks_selected_entities_and_prunes_decisions(
    fallback_entry,
):
    hass, entry = fallback_entry
    decision = {"destination": "local", "recorded_at": "2026-10-24T00:00:00+00:00"}
    hass.config_entries.async_update_entry(
        entry,
        options={
            "fallback_stt_local": True,
            "fallback_tts_local": True,
            "local_decisions": {
                "stt.contract_stt": decision,
                "tts.contract_tts": decision,
            },
        },
    )
    await hass.async_block_till_done()
    form = await start(hass, entry)
    with patch(
        "custom_components.assist_live.config_flow.entity_iot_class",
        return_value="cloud_push",
    ):
        result = await submit(
            hass,
            form,
            values(
                fallback_stt="stt.alternate_stt",
                local_only=True,
                fallback_stt_local=True,
                fallback_tts_local=True,
            ),
        )
    assert result["errors"]["fallback_stt_local"] == "cloud_destination", (
        "cloud selection refused"
    )
    with patch(
        "custom_components.assist_live.config_flow.entity_iot_class", return_value=None
    ):
        result = await submit(
            hass,
            result,
            values(
                fallback_stt="stt.alternate_stt",
                local_only=True,
                fallback_stt_local=True,
                fallback_tts_local=True,
            ),
        )
    assert result["type"] == "create_entry"
    decisions = entry.options["local_decisions"]
    assert set(decisions) == {"stt.alternate_stt", "tts.contract_tts"}, (
        "decisions follow selected entity IDs"
    )
    assert decisions["tts.contract_tts"] == decision, (
        "unchanged entity keeps original local decision"
    )
    assert decisions["stt.alternate_stt"]["recorded_at"] != decision["recorded_at"], (
        "new entity gets a new local decision"
    )


async def test_options_unchanged_entities_keep_one_step(fallback_entry):
    hass, entry = fallback_entry
    runtime = hass.data["assist_live"][entry.entry_id]
    turn = runtime.ledger.open_voice("satellite-a")
    form = await start(hass, entry)
    assert {item["value"] for item in voice_options(form)} == {
        "",
        "voice-one",
        "voice-two",
    }
    result = await submit(hass, form, values(fallback_tts_voice="voice-two"))
    assert result["type"] == "create_entry", "unchanged TTS keeps one-step options"
    assert entry.options["fallback_tts_voice"] == "voice-two"
    await hass.async_block_till_done()
    assert hass.data["assist_live"][entry.entry_id] is runtime, (
        "voice-only change does not reload"
    )
    assert runtime.ledger.current("satellite-a") is turn, (
        "voice-only change keeps running turn"
    )
    assert runtime.tts["device-a"].fallback_voice == "voice-two"
    form = await start(hass, entry)
    with patch(
        "custom_components.assist_live.config_flow.entity_iot_class",
        return_value="local_push",
    ):
        result = await submit(
            hass, form, values(fallback_tts_voice="voice-two", local_only=True)
        )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert hass.data["assist_live"][entry.entry_id] is runtime, (
        "mode-only change does not reload"
    )
    assert runtime.ledger.local_only, "mode change applies in place"
    assert runtime.ledger.current("satellite-a") is turn, (
        "mode-only change keeps running turn"
    )


@pytest.mark.parametrize(
    "field,part", [("fallback_stt", "stt"), ("fallback_tts", "tts")]
)
async def test_setup_and_options_share_missing_and_loop_checks(
    fallback_entry, field, part
):
    hass, entry = fallback_entry
    runtime = hass.data["assist_live"][entry.entry_id]
    for entity_id, error in (
        (f"{part}.missing", "invalid_fallback_entity"),
        (getattr(runtime, part)["device-a"].entity_id, "fallback_loop"),
    ):
        form = await hass.config_entries.flow.async_init(
            "assist_live", context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                "url": "https://server.example",
                "token": "test-only",
                "fallback_stt": "stt.contract_stt",
                "fallback_tts": "tts.contract_tts",
                field: entity_id,
            },
        )
        assert result["errors"].get(field) == error, (
            "setup and options use the same fallback checks"
        )


@pytest.mark.parametrize("part", ["stt", "tts"])
async def test_options_loaded_unavailable_engine_is_not_missing(fallback_entry, part):
    hass, entry = fallback_entry
    hass.states.async_set(f"{part}.alternate_{part}", "unavailable")
    form = await start(hass, entry)
    result = await submit(
        hass, form, values(**{f"fallback_{part}": f"{part}.alternate_{part}"})
    )
    assert not result.get("errors"), "loaded unavailable engine is still selectable"
    if part == "tts":
        result = await submit(hass, result, {"fallback_tts_voice": ""})
    assert result["type"] == "create_entry"


@pytest.mark.parametrize("part", ["stt", "tts"])
async def test_options_new_unknown_entity_needs_its_own_local_choice(
    fallback_entry, part
):
    hass, entry = fallback_entry
    with patch(
        "custom_components.assist_live.config_flow.entity_iot_class", return_value=None
    ):
        form = await start(hass, entry)
        form = await submit(
            hass,
            form,
            values(local_only=True, fallback_stt_local=True, fallback_tts_local=True),
        )
        assert form["type"] == "create_entry"
        form = await start(hass, entry)
        form = await submit(
            hass,
            form,
            values(
                local_only=True,
                **{
                    f"fallback_{part}": f"{part}.alternate_{part}",
                    f"fallback_{'tts' if part == 'stt' else 'stt'}_local": True,
                },
            ),
        )
    assert form["errors"].get(f"fallback_{part}_local") == "unknown_destination", (
        "new entity cannot inherit an old local decision"
    )


@pytest.mark.parametrize("part", ["stt", "tts"])
async def test_options_local_only_refuses_selected_cloud_engine(fallback_entry, part):
    hass, entry = fallback_entry
    form = await start(hass, entry)
    with patch(
        "custom_components.assist_live.config_flow.entity_iot_class",
        return_value="cloud_push",
    ):
        form = await submit(
            hass,
            form,
            values(
                local_only=True,
                fallback_stt_local=True,
                fallback_tts_local=True,
                **{
                    f"fallback_{part}": f"{part}.alternate_{part}",
                },
            ),
        )
    assert form["errors"].get(f"fallback_{part}_local") == "cloud_destination", (
        "selected cloud engine cannot use a local decision"
    )
    assert entry.options == {}, "cloud destination refusal leaves options unchanged"
