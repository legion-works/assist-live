import copy
import inspect
import json
import logging
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_pipeline_fields_bind_one_satellite():
    from tools.probe_pipelines import pipeline_fields

    fields = pipeline_fields("stt.test_one", "tts.test_one")
    assert fields["stt_engine"] == "stt.test_one"
    assert fields["tts_engine"] == "tts.test_one"


def test_pipeline_fields_include_voice_and_language_pairs():
    from tools.probe_pipelines import pipeline_fields

    fields = pipeline_fields("stt.test_one", "tts.test_one", "voice-two")
    assert fields == {
        "name": "Pipeline contract probe",
        "language": "en",
        "conversation_engine": "conversation.home_assistant",
        "conversation_language": "en",
        "stt_engine": "stt.test_one",
        "stt_language": "en",
        "tts_engine": "tts.test_one",
        "tts_language": "en",
        "tts_voice": "voice-two",
        "wake_word_entity": None,
        "wake_word_id": None,
        "prefer_local_intents": False,
    }


def test_public_creation_and_update_exports_and_signatures():
    from homeassistant.components import assist_pipeline
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import UNDEFINED, UndefinedType

    assert {"async_create_default_pipeline", "async_update_pipeline"} <= set(
        assist_pipeline.__all__
    )
    create = inspect.signature(assist_pipeline.async_create_default_pipeline)
    assert list(create.parameters) == [
        "hass",
        "stt_engine_id",
        "tts_engine_id",
        "pipeline_name",
    ]
    for name, param in create.parameters.items():
        assert param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert param.default is inspect.Parameter.empty
        assert param.annotation is (HomeAssistant if name == "hass" else str)
    assert create.return_annotation == assist_pipeline.Pipeline | None

    update = inspect.signature(assist_pipeline.async_update_pipeline)
    strings = ["conversation_engine", "conversation_language", "language", "name"]
    nullable = [
        "stt_engine",
        "stt_language",
        "tts_engine",
        "tts_language",
        "tts_voice",
        "wake_word_entity",
        "wake_word_id",
    ]
    assert list(update.parameters) == [
        "hass",
        "pipeline",
        *strings,
        *nullable,
        "prefer_local_intents",
    ]
    for name, annotation in (
        ("hass", HomeAssistant),
        ("pipeline", assist_pipeline.Pipeline),
    ):
        assert update.parameters[name].annotation is annotation
        assert update.parameters[name].default is inspect.Parameter.empty
        assert update.parameters[name].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    for name in [*strings, *nullable, "prefer_local_intents"]:
        param = update.parameters[name]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert param.default is UNDEFINED
        expected = (
            bool | UndefinedType
            if name == "prefer_local_intents"
            else (
                str | UndefinedType | None if name in nullable else str | UndefinedType
            )
        )
        assert param.annotation == expected
    assert update.return_annotation is None
    assert inspect.iscoroutinefunction(assist_pipeline.async_create_default_pipeline)
    assert inspect.iscoroutinefunction(assist_pipeline.async_update_pipeline)


@pytest.fixture
async def pipeline_hass(hass):
    from unittest.mock import AsyncMock, patch

    from aiohttp.web_exceptions import NotAppKeyWarning
    from homeassistant.components import stt, tts
    from homeassistant.components.stt.const import DATA_COMPONENT as STT_COMPONENT
    from homeassistant.components.tts import Voice
    from homeassistant.components.tts.const import DATA_COMPONENT as TTS_COMPONENT
    from homeassistant.setup import async_setup_component

    class ProbeStt(stt.SpeechToTextEntity):
        _attr_name = "Contract STT"

        @property
        def supported_languages(self):
            return ["en"]

        @property
        def supported_formats(self):
            return [stt.AudioFormats.WAV]

        @property
        def supported_codecs(self):
            return [stt.AudioCodecs.PCM]

        @property
        def supported_bit_rates(self):
            return [stt.AudioBitRates.BITRATE_16]

        @property
        def supported_sample_rates(self):
            return [stt.AudioSampleRates.SAMPLERATE_16000]

        @property
        def supported_channels(self):
            return [stt.AudioChannels.CHANNEL_MONO]

        async def async_process_audio_stream(self, metadata, stream):
            raise AssertionError("No audio is part of the pipeline contract")

    class ProbeTts(tts.TextToSpeechEntity):
        _attr_name = "Contract TTS"
        _attr_supported_languages = ["en"]
        _attr_default_language = "en"
        _attr_supported_options = ["voice"]

        def async_get_supported_voices(self, language):
            return [Voice("voice-one", "Voice one"), Voice("voice-two", "Voice two")]

    # HA 2026.10.3 http/server.py:290 uses a string application key.
    # Its pyproject.toml:457-468 does not promote this warning to an error.
    # This assertion must fail when HA removes the warning. Then remove it.
    with pytest.warns(NotAppKeyWarning, match="It is recommended to use web.AppKey"):
        assert await async_setup_component(hass, "http", {})
    with patch("haffmpeg.tools.FFVersion.get_version", AsyncMock(return_value="6.0")):
        for domain in (
            "homeassistant",
            "stt",
            "tts",
            "conversation",
            "assist_pipeline",
            "select",
        ):
            assert await async_setup_component(hass, domain, {})
    for domain, entity in (("stt", ProbeStt()), ("tts", ProbeTts())):
        component = hass.data[STT_COMPONENT if domain == "stt" else TTS_COMPONENT]
        alternate = ProbeStt() if domain == "stt" else ProbeTts()
        alternate._attr_name = "Alternate STT" if domain == "stt" else "Alternate TTS"
        await component.async_add_entities([entity, alternate])
    await hass.async_block_till_done()
    return hass


async def test_public_in_process_creation_update_and_refusal(pipeline_hass):
    from homeassistant.components import assist_pipeline

    hass = pipeline_hass
    original_ids = {p.id for p in assist_pipeline.async_get_pipelines(hass)}
    first = await assist_pipeline.async_create_default_pipeline(
        hass, "stt.contract_stt", "tts.contract_tts", "Contract one"
    )
    assert first is not None
    assert first.language == hass.config.language
    assert first.conversation_engine == "conversation.home_assistant"
    assert first.stt_language == first.tts_language == "en"
    assert first.tts_voice == "voice-one"
    second = await assist_pipeline.async_create_default_pipeline(
        hass, "stt.contract_stt", "tts.contract_tts", "Contract one"
    )
    assert second is not None and second.id != first.id
    await assist_pipeline.async_update_pipeline(
        hass,
        first,
        tts_voice="voice-two",
        stt_engine="stt.alternate_stt",
        tts_engine="tts.alternate_tts",
        name="Contract updated",
    )
    found = next(
        p for p in assist_pipeline.async_get_pipelines(hass) if p.id == first.id
    )
    assert found.tts_voice == "voice-two"
    assert found.name == "Contract updated"
    assert found.stt_engine == "stt.alternate_stt"
    assert found.tts_engine == "tts.alternate_tts"
    for stt_id, tts_id in (
        ("stt.missing", "tts.contract_tts"),
        ("stt.contract_stt", "tts.missing"),
    ):
        assert (
            await assist_pipeline.async_create_default_pipeline(
                hass, stt_id, tts_id, "Refused"
            )
            is None
        )
    hass.config.language = "de"
    assert (
        await assist_pipeline.async_create_default_pipeline(
            hass, "stt.contract_stt", "tts.contract_tts", "Unsupported language"
        )
        is None
    )
    assert len(assist_pipeline.async_get_pipelines(hass)) == len(original_ids) + 2


async def test_each_satellite_selects_its_own_pipeline(pipeline_hass):
    from homeassistant.components import assist_pipeline
    from homeassistant.components.assist_satellite import (
        AssistSatelliteConfiguration,
        AssistSatelliteEntity,
    )
    from homeassistant.helpers.entity_component import EntityComponent

    hass = pipeline_hass

    class ProbeSatellite(AssistSatelliteEntity):
        _attr_name = "Contract satellite"

        def async_get_configuration(self):
            return AssistSatelliteConfiguration([], [], 0)

        async def async_set_configuration(self, config):
            pass

        def on_pipeline_event(self, event):
            pass

    selectors = []
    satellites = []
    for number in (1, 2):
        pipeline = await assist_pipeline.async_create_default_pipeline(
            hass, "stt.contract_stt", "tts.contract_tts", f"Satellite {number}"
        )
        assert pipeline is not None
        selector = assist_pipeline.AssistPipelineSelect(
            hass, "assist_pipeline", f"test-{number}"
        )
        selector.entity_id = f"select.contract_{number}"
        selectors.append(selector)
        satellite = ProbeSatellite()
        satellite.entity_id = f"assist_satellite.contract_{number}"
        satellite._attr_pipeline_entity_id = selector.entity_id
        satellites.append(satellite)
    from homeassistant.components.select.const import DATA_COMPONENT

    component = hass.data[DATA_COMPONENT]
    await component.async_add_entities(selectors)
    component = EntityComponent(logging.getLogger(__name__), "assist_satellite", hass)
    component.register_shutdown()
    await component.async_add_entities(satellites)
    for number, satellite in enumerate(satellites, 1):
        await hass.services.async_call(
            "select",
            "select_option",
            {
                "entity_id": satellite.pipeline_entity_id,
                "option": f"Satellite {number}",
            },
            blocking=True,
        )
        assert (
            hass.states.get(satellite.pipeline_entity_id).state == f"Satellite {number}"
        )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": satellites[1].pipeline_entity_id, "option": "Satellite 1"},
        blocking=True,
    )
    assert hass.states.get(satellites[0].pipeline_entity_id).state == "Satellite 1"
    assert hass.states.get(satellites[1].pipeline_entity_id).state == "Satellite 1"


def test_check_report_and_collect_failures(tmp_path, capsys):
    from tools.probe_pipelines import main

    report = ROOT / "docs/research/q2-pipelines.md"
    assert main(["--check", "--report", str(report)]) == 0
    assert "PASS Q2 pipeline contract" in capsys.readouterr().out
    data = json.loads(report.read_text().split("```json\n")[1].split("```")[0])
    damaged = copy.deepcopy(data)
    damaged["pipeline_fields"].pop("tts_voice")
    damaged["setup"] = "private_store"
    bad = tmp_path / "bad.md"
    bad.write_text("```json\n" + json.dumps(damaged) + "\n```\n")
    assert main(["--check", "--report", str(bad)]) == 1
    output = capsys.readouterr().out
    assert "FAIL setup" in output
    assert "FAIL pipeline_fields" in output
    assert "PASS" not in output


def test_missing_report_is_a_named_failure(tmp_path, capsys):
    from tools.probe_pipelines import main

    assert main(["--check", "--report", str(tmp_path / "missing.md")]) == 1
    assert capsys.readouterr().out.startswith("FAIL report")
