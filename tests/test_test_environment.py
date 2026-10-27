import copy
from pathlib import Path

import pytest
from homeassistant.core import callback
from test_pipeline_contract import pipeline_hass as configured_hass  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def test_missing_action_trace_fails_closed():
    from tools.test_environment import validate_run_evidence

    assert validate_run_evidence({"recognized_transcript": "test"})


@pytest.fixture
def evidence():
    return {
        "recognized_transcript": "start a timer for five minutes",
        "selected_intent": {"name": "HassStartTimer", "slots": {"minutes": 5}},
        "context_id": "test-context",
        "response_type": "action_done",
        "actions": [{"kind": "timer", "event": "started", "seconds": 300}],
        "service_calls": [],
        "reply": "Done.",
        "text_deltas": [],
        "group": "short",
    }


def test_complete_evidence_and_missing_fields(evidence):
    from tools.test_environment import validate_run_evidence

    assert validate_run_evidence(evidence) == []
    for key in evidence:
        incomplete = copy.deepcopy(evidence)
        incomplete.pop(key)
        assert validate_run_evidence(incomplete), key


@pytest.mark.parametrize("actions", [[], None, [None], [{"kind": "timer"}]])
def test_empty_or_incomplete_action_fails(evidence, actions):
    from tools.test_environment import validate_run_evidence

    evidence["actions"] = actions
    assert validate_run_evidence(evidence)


def test_reply_boundaries_and_real_deltas(evidence):
    from tools.test_environment import validate_run_evidence

    evidence.update(group="long", reply="x" * 61, text_deltas=["x" * 30, "x" * 31])
    assert not validate_run_evidence(evidence)
    for deltas in ([], ["wrong"], ["x" * 61]):
        evidence["text_deltas"] = deltas
        assert validate_run_evidence(evidence)
    evidence.update(group="short", reply="x" * 60, text_deltas=[])
    assert not validate_run_evidence(evidence)
    evidence["text_deltas"] = ["x" * 60]
    assert validate_run_evidence(evidence)


def test_service_action_requires_its_public_call_record(evidence):
    from tools.test_environment import validate_run_evidence

    call = {
        "domain": "light",
        "service": "turn_on",
        "service_data": {"entity_id": "light.virtual_light"},
        "context_id": evidence["context_id"],
    }
    evidence.update(
        selected_intent={"name": "HassTurnOn", "slots": {}},
        actions=[{"kind": "service", **call}],
        service_calls=[call],
    )
    assert not validate_run_evidence(evidence)
    evidence["service_calls"] = []
    assert validate_run_evidence(evidence)


def test_timer_operation_agrees_with_selected_slots(evidence):
    from tools.test_environment import validate_run_evidence

    evidence["actions"][0]["seconds"] = 301
    assert validate_run_evidence(evidence)
    evidence["actions"][0]["seconds"] = 300
    evidence["selected_intent"]["name"] = "HassTurnOff"
    assert validate_run_evidence(evidence)


def test_label_evidence_does_not_claim_transcript_recognition(evidence):
    from tools.test_environment import validate_intent_action_evidence

    label = {
        "transcript": "a different label text",
        "intent": {"name": "HassStartTimer", "slots": {"minutes": 5}},
    }
    assert validate_intent_action_evidence(evidence, label) == []
    evidence["selected_intent"]["name"] = "HassTurnOff"
    assert validate_intent_action_evidence(evidence, label) == ["intent differs"]
    evidence["selected_intent"]["name"] = "HassStartTimer"
    evidence["actions"][0]["seconds"] = 301
    assert validate_intent_action_evidence(evidence, label) == [
        "timer duration differs"
    ]


def test_transcript_record_documentation_states_the_text_path_limit():
    text = (ROOT / "docs/testing/virtual-intents.md").read_text()
    assert (
        "The record's `recognized_transcript` is the replayed corpus `transcript`, "
        "not an STT result." in text
    )
    assert (
        "The `PASS transcript intent action evidence` line checks "
        "the intent and operation records for that text." in text
    )
    assert "Different tests measure the STT path." in text


def test_groups_are_constant_and_each_has_fifty_held_out_replays():
    from tools.corpus import load_corpus
    from tools.test_environment import reply_groups

    rows = load_corpus(ROOT / "tests/data/labels.jsonl")
    groups = reply_groups(rows, "held_out")
    assert set(groups) == {"short", "long"}
    assert all(len(group) >= 50 for group in groups.values())
    assert groups == reply_groups(list(reversed(rows)), "held_out")
    with pytest.raises(ValueError, match="reply group"):
        reply_groups([], "held_out")
    with pytest.raises(ValueError, match="reply group"):
        reply_groups(rows[:1], "held_out")
    with pytest.raises(ValueError, match="duplicate"):
        reply_groups([groups["short"][0]] * 200, "held_out")


def test_fixture_has_no_debug_api_reads(tmp_path):
    import shutil

    from tools.test_environment import validate_agent_source

    fixture = ROOT / "tests/fixtures/custom_components/assist_live_test_agent"
    assert not validate_agent_source(fixture)
    assert validate_agent_source(tmp_path)
    shutil.copytree(fixture, tmp_path, dirs_exist_ok=True)
    (tmp_path / "conversation.py").write_text(
        "async def bad(ws):\n"
        '    await ws.send_json({"type": "assist_pipeline/run/debug"})\n'
    )
    assert validate_agent_source(tmp_path) == ["agent reads a debug API"]


@pytest.mark.parametrize(
    "missing", ["__init__.py", "conversation.py", "config_flow.py"]
)
def test_each_required_agent_file_has_its_own_failure(tmp_path, missing):
    import shutil

    from tools.test_environment import validate_agent_source

    fixture = ROOT / "tests/fixtures/custom_components/assist_live_test_agent"
    shutil.copytree(fixture, tmp_path, dirs_exist_ok=True)
    assert validate_agent_source(tmp_path) == []
    (tmp_path / missing).unlink()
    assert validate_agent_source(tmp_path) == [
        f"missing agent fixture source {missing}"
    ]


def test_install_refuses_another_container_volume(monkeypatch):
    from tools.test_environment import install_virtual

    monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: "[]")
    with pytest.raises(ValueError, match="isolated test volume"):
        install_virtual()


def test_inventory_covers_the_frozen_corpus():
    import json

    from tools.corpus import load_corpus
    from tools.test_environment import validate_inventory

    rows = load_corpus(ROOT / "tests/data/labels.jsonl")
    inventory = json.loads((ROOT / "tests/fixtures/intents.json").read_text())
    assert not validate_inventory(rows, inventory)
    assert validate_inventory(rows, {})


@pytest.mark.parametrize("missing", ["light.virtual_light", "assist_live_virtual"])
def test_live_inventory_resolves_entities_and_its_timer_device(missing):
    from tools.test_environment import validate_live_inventory

    inventory = {
        "HassStartTimer": {"entities": ["assist_live_virtual"]},
        "HassTurnOn": {"entities": ["light.virtual_light"]},
    }
    states = [{"entity_id": "light.virtual_light"}]
    devices = [
        {
            "id": "virtual-device",
            "identifiers": [["assist_live_test_agent", "assist_live_virtual"]],
        }
    ]
    assert validate_live_inventory(inventory, states, devices, "virtual-device") == []
    if missing == "light.virtual_light":
        states = []
    else:
        devices[0]["id"] = "a-different-device"
    assert validate_live_inventory(inventory, states, devices, "virtual-device") == [
        f"virtual inventory target missing: {missing}"
    ]


def test_mutation_record_uses_a_reproducible_repository_command():
    text = (ROOT / "docs/verification.md").read_text()
    assert "Fifteen mutation tests found" not in text
    assert (
        "uv run --locked pytest tests/test_test_environment.py "
        "tests/test_config_flow.py tests/test_ci.py -q"
    ) in text


async def test_real_ha_agent_records_timer_and_streams(configured_hass, monkeypatch):  # noqa: F811
    from homeassistant.components import conversation
    from homeassistant.core import Context
    from homeassistant.helpers.chat_session import async_get_chat_session
    from homeassistant.setup import async_setup_component

    import custom_components

    hass = configured_hass
    monkeypatch.setattr(
        custom_components,
        "__path__",
        [str(ROOT / "tests/fixtures/custom_components"), *custom_components.__path__],
    )
    assert await async_setup_component(hass, "conversation", {})
    assert await async_setup_component(hass, "intent", {})
    from custom_components.assist_live_test_agent import register_timer
    from custom_components.assist_live_test_agent.conversation import TestAgent

    register_timer(hass)
    records = []
    hass.bus.async_listen(
        "assist_live_test_record", callback(lambda event: records.append(event.data))
    )
    for group in ("short", "long"):
        agent = TestAgent(group)
        await hass.data[conversation.DATA_COMPONENT].async_add_entities([agent])
        received_deltas = []
        with (
            async_get_chat_session(hass, None) as session,
            conversation.async_get_chat_log(
                hass,
                session,
                chat_log_delta_listener=lambda log, delta, target=received_deltas: (
                    target.append(delta)
                ),
            ),
        ):
            result = await conversation.async_converse(
                hass,
                "start a timer for five minutes",
                session.conversation_id,
                Context(),
                language="en",
                agent_id=agent.entity_id,
                device_id="assist_live_virtual",
            )
        await hass.async_block_till_done()
        from tools.test_environment import validate_run_evidence

        assert result.response.response_type.value == "action_done"
        assert len(records) == (1 if group == "short" else 2)
        assert not validate_run_evidence(records[-1]), records[-1]
        assert records[-1]["selected_intent"]["name"] == "HassStartTimer"
        assert records[-1]["actions"][0]["seconds"] == 300
        actual_text = [
            delta["content"] for delta in received_deltas if delta.get("content")
        ]
        assert actual_text == records[-1]["text_deltas"]
        assert bool(actual_text) is (group == "long")

    from homeassistant.components.homeassistant.exposed_entities import (
        async_expose_entity,
    )
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import entity_registry as er

    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"test_light": {"name": "Test light"}}}
    )
    async_expose_entity(hass, "conversation", "input_boolean.test_light", False)
    agent = hass.data[conversation.DATA_COMPONENT].get_entity(
        "conversation.assist_live_short"
    )
    denied = await conversation.async_converse(
        hass,
        "turn on test light",
        None,
        Context(),
        language="en",
        agent_id=agent.entity_id,
    )
    await hass.async_block_till_done()
    assert denied.response.response_type.value == "error"
    assert records[-1]["actions"] == []
    assert hass.states.get("input_boolean.test_light").state == "off"
    async_expose_entity(hass, "conversation", "input_boolean.test_light", True)
    allowed = await conversation.async_converse(
        hass,
        "turn on test light",
        None,
        Context(),
        language="en",
        agent_id=agent.entity_id,
    )
    await hass.async_block_till_done()
    assert allowed.response.response_type.value == "action_done"
    assert hass.states.get("input_boolean.test_light").state == "on"
    assert not validate_run_evidence(records[-1]), records[-1]
    assert records[-1]["service_calls"]
    assert records[-1]["selected_intent"]["name"] == "HassTurnOn"
    room = ar.async_get(hass).async_create("Virtual room")
    ar.async_get(hass).async_create("Other room")
    er.async_get(hass).async_update_entity("input_boolean.test_light", area_id=room.id)
    wrong_area = await conversation.async_converse(
        hass,
        "turn off test light in other room",
        None,
        Context(),
        language="en",
        agent_id=agent.entity_id,
    )
    await hass.async_block_till_done()
    assert wrong_area.response.response_type.value == "error"
    assert records[-1]["actions"] == []
    assert hass.states.get("input_boolean.test_light").state == "on"
    delayed = await conversation.async_converse(
        hass,
        "turn off test light in five minutes",
        None,
        Context(),
        language="en",
        agent_id=agent.entity_id,
        device_id="assist_live_virtual",
    )
    await hass.async_block_till_done()
    assert delayed.response.response_type.value == "action_done"
    assert hass.states.get("input_boolean.test_light").state == "on"
    assert not validate_run_evidence(records[-1]), records[-1]
    assert records[-1]["actions"][0]["kind"] == "delayed_action"
