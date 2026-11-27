from collections.abc import Iterable

from homeassistant.components import assist_pipeline
from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline
from homeassistant.core import HomeAssistant


def selected_pipelines(
    hass: HomeAssistant,
    selectors: Iterable[str],
    pipelines: list[assist_pipeline.Pipeline],
) -> dict[str, assist_pipeline.Pipeline | None]:
    first_by_name: dict[str, assist_pipeline.Pipeline] = {}
    for pipeline in pipelines:
        first_by_name.setdefault(pipeline.name, pipeline)
    states = {
        entity_id: state.state
        for entity_id in set(selectors)
        if (state := hass.states.get(entity_id)) is not None
    }
    preferred = (
        _preferred_pipeline(hass)
        if any(
            name == assist_pipeline.OPTION_PREFERRED or name not in first_by_name
            for name in states.values()
        )
        else None
    )
    return {
        entity_id: preferred
        if name == assist_pipeline.OPTION_PREFERRED
        else first_by_name.get(name, preferred)
        for entity_id, name in states.items()
    }


def _preferred_pipeline(hass: HomeAssistant) -> assist_pipeline.Pipeline | None:
    try:
        return async_get_pipeline(hass)
    except KeyError, assist_pipeline.PipelineNotFound:
        return None
