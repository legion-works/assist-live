import inspect
from annotationlib import Format

import pytest
from homeassistant.components import assist_pipeline
from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline
from test_pipeline_contract import pipeline_hass  # noqa: F401


def test_pipeline_lookup_signature():
    signature = inspect.signature(async_get_pipeline, annotation_format=Format.STRING)
    assert str(signature) == (
        "(hass: 'HomeAssistant', pipeline_id: 'str | None' = None) -> 'Pipeline'"
    )


async def test_pipeline_lookup_reads_preferred_pipeline_and_rejects_missing_id(
    pipeline_hass,  # noqa: F811
):
    hass = pipeline_hass
    (default,) = assist_pipeline.async_get_pipelines(hass)
    alternate = await assist_pipeline.async_create_default_pipeline(
        hass, "stt.contract_stt", "tts.contract_tts", "Alternate pipeline"
    )
    assert alternate is not None
    assert alternate.id != default.id
    assert async_get_pipeline(hass) == default
    assert async_get_pipeline(hass, None) == default
    assert async_get_pipeline(hass, alternate.id) == alternate
    with pytest.raises(assist_pipeline.PipelineNotFound):
        async_get_pipeline(hass, "missing-pipeline")


async def test_pipeline_lookup_without_assist_pipeline_data_raises_key_error(hass):
    with pytest.raises(KeyError):
        async_get_pipeline(hass)
