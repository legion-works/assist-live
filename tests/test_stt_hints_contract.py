import inspect
from annotationlib import Format

from homeassistant.components.homeassistant import exposed_entities as ee
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component


def test_hint_ha_interface_signatures():
    expected = [
        (
            ee.async_should_expose,
            "(hass: 'HomeAssistant', assistant: 'str', entity_id: 'str') -> 'bool'",
        ),
        (
            ee.async_listen_entity_updates,
            "(hass: 'HomeAssistant', assistant: 'str', "
            "listener: 'Callable[[], None]') -> 'CALLBACK_TYPE'",
        ),
        (
            intent.async_get_entity_aliases,
            "(hass: 'HomeAssistant', entity_entry: 'er.RegistryEntry | None', *, "
            "state: 'State', allow_empty: 'bool' = True) -> 'list[str]'",
        ),
    ]
    for function, signature in expected:
        assert (
            str(inspect.signature(function, annotation_format=Format.STRING))
            == signature
        ), f"ha_hint_signature_{function.__name__}"


async def test_hint_ha_exposure_alias_and_listener_contract(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "light", "test", "hint-contract", original_name="Contract Lamp"
    )
    registry.async_update_entity(
        entity.entity_id, aliases=[er.COMPUTED_NAME, "Contract Alias"]
    )
    hass.states.async_set(entity.entity_id, "on", {"friendly_name": "Contract Lamp"})
    state = hass.states.get(entity.entity_id)
    assert intent.async_get_entity_aliases(
        hass, registry.async_get(entity.entity_id), state=state
    ) == ["Contract Lamp", "Contract Alias"], "ha_computed_name_and_alias_contract"
    assert ee.async_should_expose(hass, "conversation", entity.entity_id), (
        "ha_default_conversation_exposure"
    )
    received = []
    unsub = ee.async_listen_entity_updates(
        hass, "conversation", lambda: received.append(True)
    )
    ee.async_expose_entity(hass, "conversation", entity.entity_id, False)
    assert received == [True], "ha_exposure_listener_contract"
    assert not ee.async_should_expose(hass, "conversation", entity.entity_id), (
        "ha_explicit_exposure_contract"
    )
    unsub()
    ee.async_expose_entity(hass, "conversation", entity.entity_id, True)
    assert received == [True], "ha_exposure_unsubscribe_contract"
    hass.states.async_set(
        "light.legacy_contract", "on", {"friendly_name": "Legacy Contract"}
    )
    legacy = hass.states.get("light.legacy_contract")
    assert intent.async_get_entity_aliases(hass, None, state=legacy) == [
        "Legacy Contract"
    ], "ha_legacy_alias_contract"
    assert ee.async_should_expose(hass, "conversation", legacy.entity_id), (
        "ha_legacy_exposure_contract"
    )
