import inspect
from annotationlib import Format

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
