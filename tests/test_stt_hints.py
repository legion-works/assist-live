import asyncio
import hashlib
import importlib
import importlib.util
import json
import logging
from datetime import timedelta
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from assist_live_client import AssistLiveClient
from homeassistant.components.homeassistant import exposed_entities as ee
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.assist_live import Runtime
from custom_components.assist_live.diagnostics import async_get_config_entry_diagnostics

EMPTY_DIGEST = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def hints_module():
    name = "custom_components.assist_live.stt_hints"
    assert importlib.util.find_spec(name) is not None, "hint_builder_missing"
    return importlib.import_module(name)


def test_name_rules_and_stable_area_priority():
    build = hints_module().build_name_hints
    entities = [
        " TEST Light ",
        "test light",
        "ALIAS",
        "",
        " \t",
        "123",
        "💡",
        "a" * 65,
        "B" * 64,
        "7 é",
        "İ",
        " Area A ",
        "x",
        "\n Name \t",
    ]
    areas = [" Zoo ", "AREA A", "area a", "1234"]
    expected = [
        "area a",
        "zoo",
        "7 é",
        "alias",
        "b" * 64,
        "i̇",
        "name",
        "test light",
        "x",
    ]
    names, not_sent = build(areas, entities)
    assert names == expected, "normalized_names_and_area_priority"
    assert build(reversed(areas), reversed(entities)) == (expected, 0), "stable_order"
    assert not_sent == 0, "invalid_names_not_counted_as_overflow"
    assert all(
        name == name.lower() == name.strip()
        and 1 <= len(name) <= 64
        and any(c.isalpha() for c in name)
        for name in names
    ), "server_name_rules_exact"
    assert build([], []) == ([], 0), "empty_names"


def test_name_limit_keeps_sorted_areas_then_entities():
    areas = [f"z-area-{n:03}" for n in range(510)]
    entities = [f"a-entity-{n:03}" for n in range(10)] + areas
    names, not_sent = hints_module().build_name_hints(reversed(areas), entities)
    assert names == areas[:500], "area_priority_before_500_limit"
    assert not_sent == 20, "overflow_counts_unique_valid_names"


def test_digest_protocol_vectors():
    digest = hints_module().hints_digest
    assert digest([]) == EMPTY_DIGEST, "protocol_empty_digest"
    assert digest(["kitchen", "test light", "éclairage"]) == (
        "d189a4dcc42ecff4fb3cfb2812d63294eb1e146f7f1eb6ede12f3e36b16cb1da"
    ), "protocol_three_name_utf8_digest"


@pytest.fixture
async def hint_runtime(hass, socket_enabled):
    assert await async_setup_component(hass, "homeassistant", {})
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "light", "test", "hint-light", original_name="Synthetic Lamp"
    )
    registry.async_update_entity(
        entity.entity_id, aliases=[er.COMPUTED_NAME, "Synthetic Alias"]
    )
    hass.states.async_set(entity.entity_id, "on", {"friendly_name": "Synthetic Lamp"})
    hass.states.async_set("light.legacy", "on", {"friendly_name": "Synthetic Legacy"})
    hass.states.async_set("sensor.hidden", "1", {"friendly_name": "Synthetic Hidden"})
    ee.async_expose_entity(hass, "conversation", "sensor.hidden", False)
    ar.async_get(hass).async_create("Synthetic Area")
    await hass.async_block_till_done()
    state = {
        "support": True,
        "accepted": {},
        "requests": [],
        "fail": False,
        "entered": asyncio.Event(),
        "release": asyncio.Event(),
        "hang": False,
        "events": [],
    }

    async def health(request):
        flags = {"stt": True}
        if state["support"] is not None:
            flags["stt_hints"] = state["support"]
        return web.json_response(
            {
                "version": 1,
                "healthy": True,
                "capabilities": flags,
                "stt_hints_by_satellite": state["accepted"],
            }
        )

    async def put(request):
        names = await request.json()
        satellite = request.match_info["satellite"]
        state["requests"].append((satellite, names))
        state["entered"].set()
        if state["hang"]:
            await state["release"].wait()
        if state["fail"]:
            return web.json_response({"error": "Synthetic Lamp"}, status=500)
        value = {
            "count": len(names),
            "digest": hashlib.sha256(
                json.dumps(names, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        state["accepted"][satellite] = value
        return web.json_response(value)

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_put("/v1/satellites/{satellite}/hints", put)

    async def accepted(request):
        if request.path == "/v1/event":
            state["events"].append(await request.json())
        return web.json_response({})

    app.router.add_post("/v1/turns", accepted)
    app.router.add_post("/v1/event", accepted)
    entry = MockConfigEntry(
        domain="assist_live",
        data={"satellites": {"device-a": "satellite-a", "device-b": "satellite-b"}},
    )
    async with (
        TestServer(app) as server,
        AssistLiveClient(str(server.make_url("")), "test-only") as client,
    ):
        runtime = Runtime(hass, entry, client)
        hass.data["assist_live"] = {entry.entry_id: runtime}
        with patch.object(runtime, "_events"), patch.object(runtime, "_maintenance"):
            await runtime.start()
        try:
            yield runtime, entry, state, entity.entity_id
        finally:
            state["release"].set()
            await runtime.stop()
            await hass.async_block_till_done()


async def refresh(hass, runtime):
    runtime._health_due = 0
    await runtime.refresh_health()
    await hass.async_block_till_done()


async def advance(hass, seconds):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_setup_sends_exposed_names_aliases_and_areas(hass, hint_runtime):
    runtime, entry, state, _ = hint_runtime
    await refresh(hass, runtime)
    expected = [
        "synthetic area",
        "synthetic alias",
        "synthetic lamp",
        "synthetic legacy",
    ]
    assert sorted(state["requests"]) == [
        ("satellite-a", expected),
        ("satellite-b", expected),
    ], "setup_sends_each_satellite"
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    for satellite in ("satellite-a", "satellite-b"):
        row = diagnostics["stt_hints"][satellite]
        assert row["count"] == 4, "hint_count"
        assert row["digest"] == state["accepted"][satellite]["digest"], "hint_digest"
        assert row["not_sent"] == 0, "hint_overflow"
        assert row["last_result"] == "sent", "hint_send_result"
        assert dt_util.parse_datetime(row["last_send"]).tzinfo is not None, (
            "hint_send_time"
        )
    await refresh(hass, runtime)
    assert len(state["requests"]) == 2, "matching_digest_does_not_resend"


@pytest.mark.parametrize("trigger", ["entity", "area", "exposure", "all"])
async def test_registry_and_exposure_share_five_second_debounce(
    hass, hint_runtime, trigger
):
    runtime, _, state, entity_id = hint_runtime
    await refresh(hass, runtime)
    state["requests"].clear()
    with patch.object(runtime, "_maintenance"):
        if trigger in ("entity", "all"):
            er.async_get(hass).async_update_entity(
                entity_id, aliases=["Replacement Alias"]
            )
        if trigger in ("area", "all"):
            ar.async_get(hass).async_create("Next Area")
        if trigger in ("exposure", "all"):
            ee.async_expose_entity(hass, "conversation", "sensor.hidden", True)
        await hass.async_block_till_done()
        await advance(hass, 4)
        assert not state["requests"], "no_send_before_five_seconds"
        await advance(hass, 6)
        assert len(state["requests"]) == 2, "one_debounced_send_per_satellite"
    names = state["requests"][0][1]
    if trigger in ("entity", "all"):
        assert "replacement alias" in names, "registry_alias_change_sent"
    if trigger in ("area", "all"):
        assert names[0] == "next area", "area_change_sent_first"
    if trigger in ("exposure", "all"):
        assert "synthetic hidden" in names, "assist_exposure_change_sent"


async def test_health_digest_mismatch_resends_only_restarted_satellite(
    hass, hint_runtime
):
    runtime, _, state, _ = hint_runtime
    await refresh(hass, runtime)
    state["requests"].clear()
    state["accepted"]["satellite-b"] = {"count": 0, "digest": EMPTY_DIGEST}
    runtime._health_due = 0
    with patch(
        "custom_components.assist_live.assist_pipeline.async_get_pipelines",
        return_value=[],
    ):
        await runtime._maintenance(None)
        await hass.async_block_till_done()
    assert [sat for sat, _ in state["requests"]] == ["satellite-b"], (
        "digest_mismatch_resends"
    )


async def test_second_change_restarts_shared_debounce(hass, hint_runtime, freezer):
    runtime, _, state, entity_id = hint_runtime
    await refresh(hass, runtime)
    state["requests"].clear()
    er.async_get(hass).async_update_entity(entity_id, aliases=["New Alias"])
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=4))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    ar.async_get(hass).async_create("New Area")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert not state["requests"], "second_source_resets_shared_deadline"
    freezer.tick(timedelta(seconds=4))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert len(state["requests"]) == 2, "latest_change_has_one_send_per_satellite"


@pytest.mark.parametrize("support", [False, None])
async def test_no_hints_sent_to_cloud_or_older_server(hass, hint_runtime, support):
    runtime, entry, state, _ = hint_runtime
    state["support"] = support
    await refresh(hass, runtime)
    await advance(hass, 6)
    assert not state["requests"], "unsupported_server_never_gets_names"
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert "stt_hints" in diagnostics, "unsupported_hint_diagnostics_present"
    assert all(
        row["last_result"] == "skipped_no_support" and row["last_send"] is None
        for row in diagnostics["stt_hints"].values()
    ), "unsupported_result"


async def test_send_failure_is_counted_retried_and_private(hass, hint_runtime, caplog):
    runtime, entry, state, _ = hint_runtime
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    state["fail"] = True
    turn = runtime.ledger.open_voice("satellite-a")
    before = runtime.ledger.snapshot()
    await refresh(hass, runtime)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics.get("stt_hints_failures") == 2, "hint_failures_counted"
    assert all(
        row["last_result"] == "failed" and row["last_send"] is None
        for row in diagnostics["stt_hints"].values()
    ), "failed_result_without_send_time"
    assert runtime.server_healthy and runtime.ledger.snapshot() == before, (
        "hint_failure_no_turn_effect"
    )
    assert runtime.ledger.current("satellite-a").turn_id == turn.turn_id, (
        "active_turn_unchanged"
    )
    assert not ir.async_get(hass).issues, "hint_failure_no_repair"
    visible = (
        caplog.text.lower()
        + json.dumps(diagnostics).lower()
        + json.dumps(state["events"]).lower()
        + str(runtime.ledger._pending).lower()
    )
    for name in (
        "synthetic area",
        "synthetic alias",
        "synthetic lamp",
        "synthetic legacy",
    ):
        assert name not in visible, (
            "hint_name_private_in_all_debug_logs_and_diagnostics"
        )
    assert "ProtocolError" in caplog.text, "only_error_class_logged"
    state["fail"] = False
    await refresh(hass, runtime)
    assert len(state["requests"]) == 4, "failed_send_retried_on_next_trigger"


async def test_unload_cancels_debounce(hass, hint_runtime):
    runtime, _, state, entity_id = hint_runtime
    await refresh(hass, runtime)
    state["requests"].clear()
    er.async_get(hass).async_update_entity(entity_id, aliases=["Last Alias"])
    await hass.async_block_till_done()
    await runtime.stop()
    await advance(hass, 6)
    assert not state["requests"], "unload_cancels_hint_debounce"


async def test_diagnostics_count_names_over_500(hass, hint_runtime):
    runtime, entry, state, _ = hint_runtime
    for n in range(510):
        entity = er.async_get(hass).async_get_or_create(
            "light", "test", f"overflow-{n}", original_name=f"Overflow Name {n:03}"
        )
        hass.states.async_set(
            entity.entity_id, "on", {"friendly_name": f"Overflow Name {n:03}"}
        )
    await hass.async_block_till_done()
    await refresh(hass, runtime)
    await advance(hass, 6)
    assert state["requests"], "overflow_list_sent"
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert all(
        row["count"] == 500 and row["not_sent"] == 14
        for row in diagnostics["stt_hints"].values()
    ), "overflow_diagnostics_count_unique_names"


async def test_support_downgrade_stops_sends_after_registry_change(hass, hint_runtime):
    runtime, entry, state, entity_id = hint_runtime
    await refresh(hass, runtime)
    state["requests"].clear()
    state["support"] = False
    er.async_get(hass).async_update_entity(entity_id, aliases=["Changed Alias"])
    await hass.async_block_till_done()
    await advance(hass, 6)
    assert not state["requests"], "change_checks_fresh_support_before_send"
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert all(
        row["last_result"] == "skipped_no_support"
        for row in diagnostics["stt_hints"].values()
    ), "downgrade_result"


async def test_hanging_hint_request_does_not_delay_fallback_turn(hass, hint_runtime):
    from homeassistant.components.tts import TTSAudioRequest
    from test_tts_forward import Fallback, entity, text

    runtime, _, state, _ = hint_runtime
    state["hang"] = True
    await runtime.refresh_health()
    await asyncio.wait_for(state["entered"].wait(), 1)
    forward = entity(runtime.client, runtime.ledger, Fallback())
    forward.hass = hass
    forward.entity_id = "tts.hint_turn"
    async with asyncio.timeout(1):
        response = await forward.async_stream_tts_audio(
            TTSAudioRequest("en", {}, text())
        )
        audio = b"".join([part async for part in response.data_gen])
    assert audio == b"fallback", "turn_finishes_while_hint_send_hangs"
    state["release"].set()
    await hass.async_block_till_done()


async def test_success_time_survives_failed_send(hass, hint_runtime):
    runtime, entry, state, _ = hint_runtime
    await refresh(hass, runtime)
    first = await async_get_config_entry_diagnostics(hass, entry)
    state["fail"] = True
    state["accepted"].clear()
    await refresh(hass, runtime)
    failed = await async_get_config_entry_diagnostics(hass, entry)
    for satellite, row in failed["stt_hints"].items():
        assert row["last_result"] == "failed", "retry_failure_result"
        assert row["last_send"] == first["stt_hints"][satellite]["last_send"], (
            "last_success_time_retained"
        )
