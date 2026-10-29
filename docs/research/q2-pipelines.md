# Pipelines for each satellite

Date: 2026-10-24.

This is a dated research record. See [setup](../setup.md) for current integration behavior.

Decision: automatic setup can use the public in-process exports in HA `2026.10.3`.
The public-interface criterion is the component `__all__` export.
This criterion does not include the TTS fallback helper in the public interface.
The developer documentation gives pipeline run instructions, but no instructions for these two setup functions.
The decision uses the export criterion.
The project is experimental and has no release.

## Automatic setup

Import these functions from `homeassistant.components.assist_pipeline`, not its storage modules:

```python
async def async_create_default_pipeline(
    hass: HomeAssistant,
    stt_engine_id: str,
    tts_engine_id: str,
    pipeline_name: str,
) -> Pipeline | None: ...


async def async_update_pipeline(
    hass: HomeAssistant,
    pipeline: Pipeline,
    *,
    conversation_engine: str | UndefinedType = UNDEFINED,
    conversation_language: str | UndefinedType = UNDEFINED,
    language: str | UndefinedType = UNDEFINED,
    name: str | UndefinedType = UNDEFINED,
    stt_engine: str | UndefinedType | None = UNDEFINED,
    stt_language: str | UndefinedType | None = UNDEFINED,
    tts_engine: str | UndefinedType | None = UNDEFINED,
    tts_language: str | UndefinedType | None = UNDEFINED,
    tts_voice: str | UndefinedType | None = UNDEFINED,
    wake_word_entity: str | UndefinedType | None = UNDEFINED,
    wake_word_id: str | UndefinedType | None = UNDEFINED,
    prefer_local_intents: bool | UndefinedType = UNDEFINED,
) -> None: ...
```

The exports are in `assist_pipeline/__init__.py:48-69`.
The signatures are in `assist_pipeline/pipeline.py:204-228,278-323`.
The contract test checks the two exports, all parameters, parameter types, defaults, and output types on each HA version in CI.

The integration must use this sequence:

1. Wait for the satellite STT and TTS entities to become available.
2. Read the saved pipeline ID for this satellite from the integration configuration.
3. Find that ID in the result of the public `async_get_pipelines(hass) -> list[Pipeline]` export.
4. For a new satellite without a saved ID, call `async_create_default_pipeline` with its engines and a unique name.
5. If the function gives `None`, show the manual fallback instructions.
6. Save the new `Pipeline.id` in the integration configuration.
7. Read the selected language and the voices from the TTS entity.
8. Call `async_update_pipeline` with the operator-selected `tts_voice`, languages, and conversation engine.
9. Find the ID again with `async_get_pipelines` to read the updated object.

Do not find an owned pipeline by name or engine pair alone.
Do not change an unrelated pipeline or the preferred pipeline.
Do not call a private store or write HA pipeline storage.

If a saved ID is missing, get operator approval before making a replacement.
The integration must have a recovery procedure for an ID-save failure after creation.

### Creation side effects

- Creation uses the built-in `conversation.home_assistant` agent.
- The resolver reads the configured HA language and country to find compatible engine languages.
- The stored pipeline language is `hass.config.language`.
- If no conversation language agrees, the resolver starts with English defaults.
- Missing engines or engines without a compatible language cause creation to give `None` before storage changes.

- A voice list makes creation select its first voice. Update must apply the voice that the operator selects.
- Creation does not refuse a name or engine pair that is in use. Each call makes a different pipeline ID.
- Creation does not change the preferred pipeline after HA initializes the pipeline store.

- HA itself makes a preferred default pipeline when it initializes an empty store. This is not the helper call.
- Update keeps the pipeline ID and replaces the stored object. The caller must read the updated object again.

- Update accepts changed engines and `tts_voice`, but only checks engine/language pairing, not provider availability or voice compatibility.

The HA harness proves creation, duplicate creation, stable IDs, changed engines, selected voice, missing-engine refusal, and unsupported-language refusal.
The live WebSocket probe proves that the preferred pipeline stays unchanged.
There is no exported preferred-pipeline getter: HA has `async_get_pipeline`, but does not include it in `__all__`.
The harness does not use that function or the private store to read preference.

## Voice contract

The TTS entity supplies `async_get_supported_voices(language: str) -> list[Voice] | None`.
The `Voice.voice_id` value is the pipeline `tts_voice` value, not the display name.
The entity must accept the `voice` option.
HA supplies that value as `request.options["voice"]` in the streaming TTS request.

The pipeline puts `tts_voice` in the TTS options in `assist_pipeline/default_pipeline.py:921-940`.
The source declares `TTSAudioRequest.options` in `tts/entity.py:36-42` and the voice methods in lines 83-103.
The live probe reads `tts/engine/voices` before it makes a pipeline.
It makes two pipelines with a non-null voice, reads them, clears and sets the voice, and reads them again.

It deletes the two pipelines in a `finally` clause and compares the pipeline IDs and preferred ID before and after.
An offline test provider in the isolated test instance supplies the engines and two voices.
No audio synthesis, paid API, debug endpoint, or device is part of this probe.

## Satellite selection

The HA harness adds two test-only satellites and two `AssistPipelineSelect` entities.
Each satellite supplies its documented `pipeline_entity_id` property.
The test calls the public `select.select_option` service for each selector and checks the resulting states.
Selectors use pipeline names as their options, not IDs. Use unique pipeline names.

The test also makes the two satellites select the same pipeline.
HA accepts that selection. HA does not supply the assist-live repair.
The harness does not run audio through the satellites.
Device trial not run: no test device at this time.

The harness test uses three constants that HA does not export in `__all__`: `stt.const.DATA_COMPONENT`, `tts.const.DATA_COMPONENT`, and `select.const.DATA_COMPONENT`.
The test adds its test-only entities to these entity components without a platform setup.
Only the test uses these constants. The integration does not use them.
The CI matrix tests each HA version that the project accepts, so a change to these constants causes a test failure.

### Necessary duplicate-selection repair test

The subsequent integration test must add two satellites with different owned pipelines.
Change the two select entities to the same assist-live pipeline.
Resolve the selected names against `async_get_pipelines` and the saved owned IDs.
Assert that the integration shows one repair issue that identifies the two satellites and the shared pipeline.
Assert that unrelated pipelines do not cause this repair.
Select the correct pipeline for one satellite and assert that the integration removes the issue.

Do the test again after a restart and after a preferred-pipeline change with a selector that uses the preferred option.
Do not use pipeline debug records to repair turn correlation.

## Manual fallback

If automatic creation gives `None`, first check the engine availability and language compatibility.
The config flow must show the correct satellite-specific entity IDs and the selected voice.
Then the operator can use these steps:

1. Open **Settings > Voice assistants**.
2. Add a voice assistant with a unique name for this satellite.
3. Select the pipeline language and a compatible conversation agent and conversation language.
4. Select `stt.assist_live_<satellite>` as speech-to-text and a compatible language.
5. Select `tts.assist_live_<satellite>` as text-to-speech and a compatible language.
6. Select a voice from that TTS entity. Use its voice ID as `tts_voice`.
7. Keep the wake-word fields unset unless a pipeline wake-word engine is necessary for the device.
8. Save the pipeline.
9. Select its unique name with this satellite's pipeline select entity.
10. Give each other satellite its own pipeline and engine pair.

The fields below are a synthetic contract example, not live entity IDs.
The report checker checks this record without a server or credentials.
It does not run the live probe again or prove the report's historical claims.

```json
{
  "ha_version": "2026.10.3",
  "setup": "automatic",
  "public_criterion": "component __all__ export",
  "exports": ["async_create_default_pipeline", "async_update_pipeline"],
  "pipeline_fields": {
    "name": "Pipeline contract probe",
    "language": "en",
    "conversation_engine": "conversation.home_assistant",
    "conversation_language": "en",
    "stt_engine": "stt.test_one",
    "stt_language": "en",
    "tts_engine": "tts.test_one",
    "tts_language": "en",
    "tts_voice": "voice-two",
    "wake_word_entity": null,
    "wake_word_id": null,
    "prefer_local_intents": false
  },
  "live": {
    "create": true,
    "get": true,
    "update": true,
    "voice_round_trip": true,
    "cleanup": true,
    "preferred_unchanged": true
  },
  "satellite_selection": "HA harness: two select entities",
  "duplicate_repair": "required: not implemented"
}
```

For a live probe, give `tools/probe_pipelines.py` the `--url`, `--token-file`, `--stt-engine`, `--tts-engine`, and `--tts-voice` arguments.
The token file must contain a JSON `token` string.
The script has no default server or credential path and accepts only the isolated loopback test address.
Do not keep the token file in the repository. Do not put tokens in command arguments.

## Sources

Access date for all sources: 2026-10-24.
The installed HA `2026.10.3` wheel supplied the source findings and line numbers.
Developer documentation is not version-pinned. The source URLs below pin the commit for HA `2026.10.3`.

- [Pipeline exports, lines 48-69](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/assist_pipeline/__init__.py#L48-L69).
- [Pipeline language resolver, lines 87-187](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/assist_pipeline/pipeline.py#L87-L187).
- [Creation and update, lines 204-323](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/assist_pipeline/pipeline.py#L204-L323).
- [Storage side effects, lines 341-421](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/assist_pipeline/pipeline.py#L341-L421).
- [WebSocket fields, lines 70-84](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/assist_pipeline/pipeline.py#L70-L84).
- [WebSocket item IDs and complete update schema, lines 584-658](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/helpers/collection.py#L584-L658).
- [TTS voice interface, lines 36-103](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/tts/entity.py#L36-L103).
- [Streaming request options, lines 1135-1137](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/tts/__init__.py#L1135-L1137).
- [Pipeline voice option, lines 921-940](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/assist_pipeline/default_pipeline.py#L921-L940).
- [Satellite name-to-ID resolution, lines 603-621](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/assist_satellite/entity.py#L603-L621).
- [HA pipeline developer documentation](https://developers.home-assistant.io/docs/voice/pipelines/).
- [HA satellite developer documentation](https://developers.home-assistant.io/docs/core/entity/assist-satellite/).
- [TTS developer documentation](https://developers.home-assistant.io/docs/core/entity/tts/).
