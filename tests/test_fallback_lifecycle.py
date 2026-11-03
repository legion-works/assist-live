import asyncio
from unittest.mock import patch

import pytest
from homeassistant.components import stt
from homeassistant.components.tts import TTSAudioRequest
from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_options_fallback import fallback_entry  # noqa: F401
from test_pipeline_contract import pipeline_hass  # noqa: F401
from test_stt_forward import Fallback as SttFallback
from test_stt_forward import audio, metadata
from test_tts_forward import Fallback as TtsFallback
from test_tts_forward import text

from custom_components.assist_live import Runtime
from custom_components.assist_live.diagnostics import async_get_config_entry_diagnostics


async def test_unavailable_reason_stays_local_when_client_refuses_marker(ledger):
    from unittest.mock import AsyncMock

    from assist_live_client.models import TimingEvent

    turn = ledger.open_voice("satellite-a")
    ledger.fallback_policy = AsyncMock(side_effect=HomeAssistantError("policy refused"))

    def refuse(*args, **kwargs):
        if kwargs.get("data", {}).get("reason") == "fallback_unavailable":
            raise ValueError("unsupported cause")
        return TimingEvent(*args, **kwargs)

    with patch("custom_components.assist_live.ledger.TimingEvent", side_effect=refuse):
        await ledger.use_fallback(
            turn.turn_id, "stt_fallback", "stt.standard", reason="fallback_unavailable"
        )
        ledger.mark(turn.turn_id, "stt_failed")
    assert ledger.fallback_reasons["fallback_unavailable"] == 1, "LOCAL_CAUSE_LOST"
    assert (
        ledger.records[turn.turn_id].fallback_reasons["stt"] == "fallback_unavailable"
    )
    assert "stt_failed" in ledger.records[turn.turn_id].stages
    assert not any(
        event.stage in ("fallback_used", "stt_failed") for event in ledger._pending
    ), "UNACCEPTED_MARKER_SENT"
    ledger.fallback_policy.assert_not_awaited()


@pytest.fixture
async def lifecycle_entry(fallback_entry):  # noqa: F811
    hass, entry = fallback_entry
    original_stt = stt.async_get_speech_to_text_entity(hass, "stt.contract_stt")
    from custom_components.assist_live.tts import fallback_engine

    original_tts = fallback_engine(hass, "tts.contract_tts")
    with (
        patch.object(
            original_stt,
            "async_process_audio_stream",
            side_effect=SttFallback().async_process_audio_stream,
        ),
        patch.object(
            original_tts,
            "async_stream_tts_audio",
            side_effect=TtsFallback().async_stream_tts_audio,
        ),
    ):
        yield hass, entry


def issue(hass, entry, part):
    return ir.async_get(hass).async_get_issue(
        "assist_live", f"{entry.entry_id}_fallback_{part}_unavailable"
    )


@pytest.mark.parametrize("part", ["stt", "tts"])
@pytest.mark.parametrize("read", ["property", "maintenance"])
async def test_startup_reads_defer_missing_engine_repair(lifecycle_entry, part, read):
    hass, entry = lifecycle_entry
    runtime = hass.data["assist_live"][entry.entry_id]
    forward = getattr(runtime, part)["device-a"]
    target = (
        "homeassistant.components.stt.async_get_speech_to_text_entity"
        if part == "stt"
        else "custom_components.assist_live.tts.get_engine_instance"
    )
    with patch(target, return_value=None):
        with patch.object(hass, "state", CoreState.starting):
            if read == "property":
                assert "en" in forward.supported_languages
                if part == "tts":
                    assert forward.default_language == "en"
                    forward.async_get_supported_voices("en")
                assert issue(hass, entry, part) is None, "BOOT_PROPERTY_CREATED_REPAIR"
            else:
                runtime._health_due = 0
                await runtime.refresh_health()
                assert issue(hass, entry, part) is None, (
                    "BOOT_MAINTENANCE_CREATED_REPAIR"
                )
        assert hass.state is CoreState.running
        assert "en" in forward.supported_languages
        assert issue(hass, entry, part) is None, "RUNNING_PROPERTY_CREATED_REPAIR"
        runtime._health_due = 0
        await runtime.refresh_health()
        assert issue(hass, entry, part) is not None, (
            "RUNNING_MAINTENANCE_REPAIR_MISSING"
        )


@pytest.mark.parametrize("part", ["stt", "tts"])
async def test_startup_turn_creates_missing_engine_repair(lifecycle_entry, part):
    hass, entry = lifecycle_entry
    runtime = hass.data["assist_live"][entry.entry_id]
    forward = getattr(runtime, part)["device-a"]
    target = (
        "homeassistant.components.stt.async_get_speech_to_text_entity"
        if part == "stt"
        else "custom_components.assist_live.tts.get_engine_instance"
    )
    with (
        patch(target, return_value=None),
        patch.object(hass, "state", CoreState.starting),
    ):
        if part == "stt":
            result = await forward.async_process_audio_stream(
                metadata(), audio([bytes(320)])
            )
            assert result.result == stt.SpeechResultState.ERROR
        else:
            with pytest.raises(HomeAssistantError, match="^fallback TTS unavailable$"):
                await forward.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
        record = runtime.ledger.latest["satellite-a"]
        assert record.fallback_reasons[part] == "fallback_unavailable", (
            "BOOT_TURN_FALLBACK_CAUSE_MISSING"
        )
        assert issue(hass, entry, part) is not None, "BOOT_TURN_REPAIR_MISSING"
        runtime._health_due = 0
        await runtime.refresh_health()
        assert issue(hass, entry, part) is not None, "BOOT_TURN_REPAIR_DELETED"


@pytest.mark.parametrize("part", ["stt", "tts"])
async def test_loaded_unavailable_engine_is_selectable_but_not_used(
    lifecycle_entry, part
):
    from custom_components.assist_live.config_flow import fallback_errors
    from custom_components.assist_live.settings import effective_settings
    from custom_components.assist_live.tts import get_engine_instance

    hass, entry = lifecycle_entry
    engine = (
        stt.async_get_speech_to_text_entity(hass, "stt.contract_stt")
        if part == "stt"
        else get_engine_instance(hass, "tts.contract_tts")
    )
    runtime = hass.data["assist_live"][entry.entry_id]
    with patch.object(engine, "_attr_available", False, create=True):
        assert fallback_errors(hass, effective_settings(entry)) == {}, (
            "LOADED_UNAVAILABLE_ENGINE_NOT_SELECTABLE"
        )
        if part == "stt":
            result = await runtime.stt["device-a"].async_process_audio_stream(
                metadata(), audio([bytes(320)])
            )
            assert result.result == stt.SpeechResultState.ERROR, "UNAVAILABLE_STT_USED"
        else:
            with pytest.raises(HomeAssistantError, match="^fallback TTS unavailable$"):
                await runtime.tts["device-a"].async_stream_tts_audio(
                    TTSAudioRequest("en", {}, text())
                )
        assert runtime.ledger.latest["satellite-a"].fallback_reasons[part] == (
            "fallback_unavailable"
        )


@pytest.mark.parametrize("part", ["stt", "tts"])
async def test_missing_fallback_turn_and_recovery(lifecycle_entry, part):
    hass, entry = lifecycle_entry
    runtime = hass.data["assist_live"][entry.entry_id]
    forward = getattr(runtime, part)["device-a"]
    target = (
        "homeassistant.components.stt.async_get_speech_to_text_entity"
        if part == "stt"
        else "custom_components.assist_live.tts.get_engine_instance"
    )
    assert "en" in forward.supported_languages
    with patch(target, return_value=None):
        if part == "stt":
            result = await forward.async_process_audio_stream(
                metadata(), audio([bytes(320)])
            )
            assert result.result == stt.SpeechResultState.ERROR, "MISSING_STT_ACCEPTED"
        else:
            try:
                await forward.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
            except HomeAssistantError as error:
                assert str(error) == "fallback TTS unavailable", "UNNAMED_TTS_ERROR"
            else:
                pytest.fail("MISSING_TTS_ACCEPTED")
        record = runtime.ledger.latest["satellite-a"]
        assert record.fallback_reasons[part] == "fallback_unavailable", (
            "MISSING_FALLBACK_CAUSE"
        )
        assert record.timings.get("fallback_used", {}).get("reason") == (
            "fallback_unavailable"
        ), "PINNED_CLIENT_REFUSES_NEW_CAUSE"
        assert f"{part}_failed" in record.stages
        assert issue(hass, entry, part) is not None, "MISSING_FALLBACK_REPAIR"
        assert issue(hass, entry, part).translation_placeholders == {
            "entity": f"{part}.contract_{part}"
        }
        assert "en" in forward.supported_languages, "LOST_LAST_KNOWN_LANGUAGE"
        data = await async_get_config_entry_diagnostics(hass, entry)
        assert data["fallback_reasons"]["fallback_unavailable"] == 1
    replacement = SttFallback() if part == "stt" else TtsFallback()
    replacement.entity_id = f"{part}.contract_{part}"
    with patch(target, return_value=replacement):
        if part == "stt":
            result = await forward.async_process_audio_stream(
                metadata(), audio([bytes(320)])
            )
            assert result.text == "test command", "STT_RECOVERY_NOT_USED"
        else:
            response = await forward.async_stream_tts_audio(
                TTSAudioRequest("en", {}, text())
            )
            assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
        assert issue(hass, entry, part) is None, "RECOVERY_REPAIR_NOT_DELETED"


@pytest.mark.parametrize("part", ["stt", "tts"])
async def test_reloaded_fallback_object_used(lifecycle_entry, part):
    hass, entry = lifecycle_entry
    runtime = hass.data["assist_live"][entry.entry_id]
    forward = getattr(runtime, part)["device-a"]
    replacement = SttFallback() if part == "stt" else TtsFallback()
    replacement.entity_id = f"{part}.contract_{part}"
    target = (
        "homeassistant.components.stt.async_get_speech_to_text_entity"
        if part == "stt"
        else "custom_components.assist_live.tts.get_engine_instance"
    )
    with patch(target, return_value=replacement):
        if part == "stt":
            result = await forward.async_process_audio_stream(
                metadata(), audio([bytes(320)])
            )
            assert result.text == "test command", "REMOVED_STT_OBJECT_USED"
            assert hasattr(replacement, "audio"), "REPLACEMENT_STT_NOT_USED"
        else:
            response = await forward.async_stream_tts_audio(
                TTSAudioRequest("en", {}, text())
            )
            assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
            assert hasattr(replacement, "text"), "REPLACEMENT_TTS_NOT_USED"


async def test_setup_succeeds_with_both_fallbacks_absent(
    pipeline_hass,  # noqa: F811
    client,
    enable_custom_integrations,
):
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
            "fallback_stt": "stt.not_started",
            "fallback_tts": "tts.not_started",
            "satellites": {"device-a": "satellite-a"},
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id), (
            "ABSENT_FALLBACK_BLOCKS_SETUP"
        )
        try:
            runtime = hass.data["assist_live"][entry.entry_id]
            runtime._health_due = 0
            await runtime.refresh_health()
            for part in ("stt", "tts"):
                forward = getattr(runtime, part)["device-a"]
                assert hass.states.get(forward.entity_id) is not None
                assert forward.supported_languages == [], "INVENTED_FALLBACK_LANGUAGE"
                assert issue(hass, entry, part), "STARTUP_REPAIR_MISSING"
            result = await runtime.stt["device-a"].async_process_audio_stream(
                metadata(), audio([bytes(320)])
            )
            assert result.result == stt.SpeechResultState.ERROR
            assert runtime.ledger.latest["satellite-a"].fallback_reasons["stt"] == (
                "fallback_unavailable"
            ), "FIRST_MISSING_STT_CAUSE_LOST"
            with pytest.raises(HomeAssistantError, match="^fallback TTS unavailable$"):
                await runtime.tts["device-a"].async_stream_tts_audio(
                    TTSAudioRequest("en", {}, text())
                )
            assert runtime.ledger.latest["satellite-a"].fallback_reasons["tts"] == (
                "fallback_unavailable"
            ), "FIRST_MISSING_TTS_CAUSE_LOST"
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "case",
    [
        "different_entity",
        "malformed_container",
        "malformed_entries",
        "malformed_languages",
        "malformed_optional",
        "missing_fields",
        "unknown_part",
    ],
)
async def test_stored_metadata_edges(
    pipeline_hass,  # noqa: F811
    client,
    enable_custom_integrations,
    case,
):
    from homeassistant.helpers.storage import Store

    hass = pipeline_hass
    cases = {
        "different_entity": {
            "stt": {"entity_id": "stt.other_engine", "languages": ["zz"]},
            "tts": {
                "entity_id": "tts.other_engine",
                "languages": ["zz"],
                "default_language": "zz",
                "default_options": {"voice": "other-voice"},
            },
        },
        "malformed_container": "invalid",
        "malformed_entries": {"stt": False, "tts": []},
        "malformed_languages": {
            "stt": {"entity_id": "stt.not_started", "languages": "en"},
            "tts": {"entity_id": "tts.not_started", "languages": [1]},
        },
        "malformed_optional": {
            "stt": {
                "entity_id": "stt.not_started",
                "languages": ["en"],
                "default_language": 1,
            },
            "tts": {
                "entity_id": "tts.not_started",
                "languages": ["en"],
                "default_options": {"voice": 1},
            },
        },
        "missing_fields": {
            "stt": {"languages": ["en"]},
            "tts": {"entity_id": "tts.not_started"},
        },
        "unknown_part": {
            "stt": {"entity_id": "stt.not_started", "languages": ["en"]},
            "tts": {
                "entity_id": "tts.not_started",
                "languages": ["en"],
                "default_language": "en",
            },
            "future_part": None,
        },
    }

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "url": "https://server.example",
            "token": "test-only",
            "fallback_stt": "stt.not_started",
            "fallback_tts": "tts.not_started",
            "satellites": {"device-a": "satellite-a"},
        },
    )
    entry.add_to_hass(hass)
    store = Store(hass, 1, f"assist_live.{entry.entry_id}.turns")
    await store.async_save(
        {"version": 1, "turns": [], "cursors": {}, "fallback_metadata": cases[case]}
    )
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id), (
            "STORED_METADATA_BLOCKED_SETUP"
        )
        try:
            runtime = hass.data["assist_live"][entry.entry_id]
            expected = ["en"] if case == "unknown_part" else []
            for part in ("stt", "tts"):
                forward = getattr(runtime, part)["device-a"]
                assert forward.supported_languages == expected, (
                    "VALID_METADATA_PART_LOST"
                    if case == "unknown_part"
                    else "INVALID_OR_DIFFERENT_ENTITY_METADATA_USED"
                )
                if case != "unknown_part":
                    assert runtime.fallback_metadata[part] == {
                        "entity_id": f"{part}.not_started",
                        "languages": [],
                    }, "INVALID_METADATA_FIELDS_RETAINED"
            invalid = ir.async_get(hass).async_get_issue(
                "assist_live", f"{entry.entry_id}_invalid_stored_state"
            )
            if case in ("different_entity", "unknown_part"):
                assert invalid is None, "IGNORED_METADATA_MARKED_STORE_INVALID"
            else:
                assert invalid is not None, "MALFORMED_METADATA_NOT_REPORTED"
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reload_clears_old_fallback_repair_before_new_setup(fallback_entry):  # noqa: F811
    import custom_components.assist_live as integration

    hass, entry = fallback_entry
    runtime = hass.data["assist_live"][entry.entry_id]
    with (
        patch(
            "homeassistant.components.stt.async_get_speech_to_text_entity",
            return_value=None,
        ),
        patch(
            "custom_components.assist_live.tts.get_engine_instance", return_value=None
        ),
    ):
        runtime._health_due = 0
        await runtime.refresh_health()
    for part in ("stt", "tts"):
        assert issue(hass, entry, part).translation_placeholders == {
            "entity": f"{part}.contract_{part}"
        }

    before_setup = []
    original_setup = integration.async_setup_entry

    async def setup(hass, entry):
        before_setup.append({part: issue(hass, entry, part) for part in ("stt", "tts")})
        return await original_setup(hass, entry)

    with patch.object(integration, "async_setup_entry", side_effect=setup):
        hass.config_entries.async_update_entry(
            entry,
            options={
                "fallback_stt": "stt.alternate_stt",
                "fallback_tts": "tts.alternate_tts",
            },
        )
        await hass.async_block_till_done()
    assert len(before_setup) == 1, "OPTIONS_CHANGE_DID_NOT_RELOAD"
    assert all(value is None for value in before_setup[0].values()), (
        "UNLOAD_RETAINED_OLD_FALLBACK_REPAIR"
    )
    reloaded = hass.data["assist_live"][entry.entry_id]
    assert reloaded is not runtime
    for part in ("stt", "tts"):
        assert issue(hass, entry, part) is None, "HEALTHY_ENGINE_RECREATED_REPAIR"

    with (
        patch(
            "homeassistant.components.stt.async_get_speech_to_text_entity",
            return_value=None,
        ),
        patch(
            "custom_components.assist_live.tts.get_engine_instance", return_value=None
        ),
    ):
        reloaded._health_due = 0
        await reloaded.refresh_health()
    for part in ("stt", "tts"):
        assert issue(hass, entry, part).translation_placeholders == {
            "entity": f"{part}.alternate_{part}"
        }, "RELOADED_REPAIR_NAMES_OLD_ENGINE"


async def test_setup_error_unloads_platforms_and_next_setup_succeeds(
    pipeline_hass,  # noqa: F811
    client,
    enable_custom_integrations,
):
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
            "satellites": {"device-a": "satellite-a"},
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        with patch.object(
            Runtime, "_maintenance", side_effect=RuntimeError("test failure")
        ):
            assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.entry_id not in hass.data["assist_live"], "FAILED_RUNTIME_RETAINED"
        registered = er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        assert registered
        assert all(
            hass.data[entity.domain].get_entity(entity.entity_id) is None
            for entity in registered
        ), "FORWARDED_PLATFORM_LEFT_REGISTERED"
        assert await hass.config_entries.async_reload(entry.entry_id), (
            "SECOND_SETUP_BLOCKED_BY_REGISTERED_PLATFORM"
        )
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("part", ["stt", "tts"])
async def test_maintenance_repairs_fallback_recovery(fallback_entry, part):  # noqa: F811
    hass, entry = fallback_entry
    runtime = hass.data["assist_live"][entry.entry_id]
    target = (
        "homeassistant.components.stt.async_get_speech_to_text_entity"
        if part == "stt"
        else "custom_components.assist_live.tts.get_engine_instance"
    )
    with patch(target, return_value=None):
        runtime._health_due = 0
        await runtime.refresh_health()
        assert issue(hass, entry, part), "MAINTENANCE_REPAIR_MISSING"
    runtime._health_due = 0
    await runtime.refresh_health()
    assert issue(hass, entry, part) is None, "MAINTENANCE_REPAIR_NOT_DELETED"


async def test_stored_metadata_allows_server_turn_before_engines_start(
    pipeline_hass,  # noqa: F811
    client,
    enable_custom_integrations,
):
    from dataclasses import replace

    from assist_live_client.models import HealthCapabilities, TTSAudio, TTSFormat
    from homeassistant.components import assist_pipeline
    from homeassistant.components.assist_pipeline import PipelineStage
    from homeassistant.core import Context
    from homeassistant.helpers.storage import Store

    hass = pipeline_hass

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    async def transcribe(*args):
        yield {"type": "final", "text": "server command"}

    async def synth(*args, **kwargs):
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(bytes(2))

    client.events = events
    client.stt = transcribe
    client.tts_stream = synth
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "url": "https://server.example",
            "token": "test-only",
            "fallback_stt": "stt.not_started",
            "fallback_tts": "tts.not_started",
            "satellites": {"device-a": "satellite-a"},
        },
    )
    entry.add_to_hass(hass)
    store = Store(hass, 1, f"assist_live.{entry.entry_id}.turns")
    await store.async_save(
        {
            "version": 1,
            "turns": [],
            "cursors": {},
            "fallback_metadata": {
                "stt": {"entity_id": "stt.not_started", "languages": ["en"]},
                "tts": {
                    "entity_id": "tts.not_started",
                    "languages": ["en"],
                    "default_language": "en",
                    "default_options": {"voice": "voice-1"},
                },
            },
        }
    )
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            runtime = hass.data["assist_live"][entry.entry_id]
            forward_stt = runtime.stt["device-a"]
            forward_tts = runtime.tts["device-a"]
            assert forward_stt.supported_languages == ["en"], "STORED_STT_METADATA_LOST"
            assert forward_tts.supported_languages == ["en"], "STORED_TTS_METADATA_LOST"
            pipeline_id = entry.data["pipelines"]["device-a"]
            pipeline = next(
                pipeline
                for pipeline in assist_pipeline.async_get_pipelines(hass)
                if pipeline.id == pipeline_id
            )
            assert pipeline.stt_language == pipeline.tts_language == "en", (
                "STORED_METADATA_NOT_VALIDATED_BY_HA"
            )
            pipeline_events = []
            await assist_pipeline.async_pipeline_from_audio_stream(
                hass,
                context=Context(),
                event_callback=pipeline_events.append,
                stt_metadata=metadata(),
                stt_stream=audio([bytes(320)]),
                pipeline_id=pipeline_id,
                end_stage=PipelineStage.STT,
            )
            assert any(
                event.type == "stt-end"
                and event.data["stt_output"]["text"] == "server command"
                for event in pipeline_events
            ), "SERVER_STT_BLOCKED_BY_ABSENT_ENGINE"
            response = await forward_tts.async_stream_tts_audio(
                TTSAudioRequest("en", {}, text())
            )
            assert b"".join([chunk async for chunk in response.data_gen])
            replacement_stt, replacement_tts = SttFallback(), TtsFallback()
            replacement_stt.supported_languages = ["de"]
            replacement_tts._attr_supported_languages = ["de"]
            replacement_tts._attr_default_language = "de"
            replacement_tts._attr_default_options = {"voice": "voice-2"}
            with (
                patch(
                    "homeassistant.components.stt.async_get_speech_to_text_entity",
                    return_value=replacement_stt,
                ),
                patch(
                    "custom_components.assist_live.tts.get_engine_instance",
                    return_value=replacement_tts,
                ),
            ):
                assert forward_stt.supported_languages == ["de"], (
                    "STT_METADATA_NOT_UPDATED"
                )
                assert forward_tts.supported_languages == ["de"], (
                    "TTS_METADATA_NOT_UPDATED"
                )
                client.health_capabilities.return_value = HealthCapabilities(False)
                result = await forward_stt.async_process_audio_stream(
                    replace(metadata(), language="de"), audio([bytes(320)])
                )
                assert result.text == "test command", "NEW_STT_LANGUAGE_NOT_USED"
                response = await forward_tts.async_stream_tts_audio(
                    TTSAudioRequest("de", {}, text())
                )
                assert (
                    b"".join([chunk async for chunk in response.data_gen])
                    == b"fallback"
                )
                assert replacement_tts.voice == "voice-2", "NEW_DEFAULT_VOICE_NOT_USED"
                await runtime._save()
                saved = await runtime.store.async_load()
                assert saved["fallback_metadata"]["stt"]["languages"] == ["de"]
                assert saved["fallback_metadata"]["tts"]["languages"] == ["de"]
                assert saved["fallback_metadata"]["tts"]["default_options"] == {
                    "voice": "voice-2"
                }, "TTS_VOICE_METADATA_NOT_SAVED"
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)
