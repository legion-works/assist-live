import inspect
from annotationlib import Format
from collections.abc import AsyncGenerator
from dataclasses import MISSING, fields
from typing import Any, get_type_hints

from homeassistant.components.tts.helper import get_engine_instance


def test_tts_lookup_signature():
    signature = inspect.signature(get_engine_instance, annotation_format=Format.STRING)
    assert list(signature.parameters) == ["hass", "engine"]
    assert all(
        p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
        and p.default is inspect.Parameter.empty
        for p in signature.parameters.values()
    )
    assert signature.parameters["hass"].annotation == "HomeAssistant"
    assert signature.parameters["engine"].annotation == "str"
    assert signature.return_annotation == "TextToSpeechEntity | Provider | None"


def test_tts_request_import_path_and_fields():
    from homeassistant.components.tts.entity import TTSAudioRequest

    from custom_components.assist_live.tts import TTSAudioRequest as ImportedRequest

    assert ImportedRequest is TTSAudioRequest
    assert TTSAudioRequest.__module__ == "homeassistant.components.tts.entity"
    assert [field.name for field in fields(TTSAudioRequest)] == [
        "language",
        "options",
        "message_gen",
    ]
    assert all(field.default is MISSING for field in fields(TTSAudioRequest))
    assert get_type_hints(TTSAudioRequest) == {
        "language": str,
        "options": dict[str, Any],
        "message_gen": AsyncGenerator[str],
    }


def test_audio_processing_import_path_and_fields():
    from homeassistant.components.stt.models import SpeechAudioProcessing

    from custom_components.assist_live.stt import (
        SpeechAudioProcessing as ImportedProcessing,
    )

    assert ImportedProcessing is SpeechAudioProcessing
    assert SpeechAudioProcessing.__module__ == "homeassistant.components.stt.models"
    assert [field.name for field in fields(SpeechAudioProcessing)] == [
        "requires_external_vad",
        "prefers_auto_gain_enabled",
        "prefers_noise_reduction_enabled",
    ]
    assert all(field.default is MISSING for field in fields(SpeechAudioProcessing))
    assert get_type_hints(SpeechAudioProcessing) == {
        "requires_external_vad": bool,
        "prefers_auto_gain_enabled": bool,
        "prefers_noise_reduction_enabled": bool,
    }
