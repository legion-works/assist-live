from unittest.mock import AsyncMock, patch

import pytest
from test_pipeline_contract import pipeline_hass as configured_hass  # noqa: F401


async def test_flow_refuses_fallback_loop(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    with patch(
        "custom_components.assist_live.config_flow.is_forwarder", return_value=True
    ):
        result = await flow.async_step_user(
            {
                "url": "http://server.example",
                "token": "test-only",
                "fallback_stt": "stt.forward",
                "fallback_tts": "tts.standard",
            }
        )
    assert result["errors"] == {"base": "fallback_loop"}


async def test_provisioned_unique_device_bindings(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.bindings = {"satellite-a", "satellite-b"}
    flow.settings = {}
    with patch(
        "custom_components.assist_live.config_flow.valid_satellite", return_value=True
    ):
        result = await flow.async_step_satellite(
            {
                "device": "device-a",
                "satellite": "satellite-a",
                "pipeline_select": "select.pipeline_a",
                "add_another": True,
            }
        )
        assert result["step_id"] == "satellite"
        result = await flow.async_step_satellite(
            {
                "device": "device-b",
                "satellite": "satellite-a",
                "pipeline_select": "select.pipeline_b",
                "add_another": True,
            }
        )
        assert result["errors"] == {"base": "binding_used"}
        result = await flow.async_step_satellite(
            {
                "device": "device-b",
                "satellite": "unknown",
                "pipeline_select": "select.pipeline_b",
                "add_another": True,
            }
        )
        assert result["errors"] == {"base": "invalid_satellite"}


async def test_user_validates_server_and_advances(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    client = AsyncMock()
    client.bindings.return_value = [{"satellite": "satellite-a"}]
    with (
        patch(
            "custom_components.assist_live.config_flow.is_forwarder", return_value=False
        ),
        patch(
            "custom_components.assist_live.config_flow.AssistLiveClient",
            return_value=client,
        ),
    ):
        result = await flow.async_step_user(
            {
                "url": "https://server.example",
                "token": "test-only",
                "fallback_stt": "stt.standard",
                "fallback_tts": "tts.standard",
            }
        )
    assert result["step_id"] == "satellite"
    assert flow.bindings == {"satellite-a"}
    client.close.assert_awaited_once()


async def test_malformed_url_has_its_own_error(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    result = await flow.async_step_user(
        {
            "url": "not-a-server",
            "token": "test-only",
            "fallback_stt": "stt.standard",
            "fallback_tts": "tts.standard",
        }
    )
    assert result["errors"] == {"base": "invalid_url"}


@pytest.mark.parametrize("url", ["http://server.example", "HTTP://server.example"])
async def test_http_warns_and_can_continue_without_refusal(hass, url):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    with patch(
        "assist_live_client.AssistLiveClient.bindings",
        return_value=[{"satellite": "satellite-a"}],
    ) as bindings:
        result = await flow.async_step_user(
            {
                "url": url,
                "token": "test-only",
                "fallback_stt": "stt.standard",
                "fallback_tts": "tts.standard",
            }
        )
        assert result["step_id"] == "http_warning"
        bindings.assert_not_awaited()
        result = await flow.async_step_http_warning({})
        assert result["step_id"] == "satellite"
        bindings.assert_awaited_once()


async def test_binding_cannot_be_reused_by_another_entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    existing = MockConfigEntry(
        domain="assist_live", data={"satellites": {"device-a": "satellite-a"}}
    )
    existing.add_to_hass(hass)
    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.bindings = {"satellite-a"}
    with patch(
        "custom_components.assist_live.config_flow.valid_satellite", return_value=True
    ):
        result = await flow.async_step_satellite(
            {
                "device": "device-b",
                "satellite": "satellite-a",
                "pipeline_select": "select.pipeline_b",
                "add_another": False,
            }
        )
    assert result["errors"] == {"base": "binding_used"}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_runtime_subscribes_each_key_before_first_turn(hass, client):
    import asyncio

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime

    entered = {key: asyncio.Event() for key in ("satellite-a", "satellite-b")}
    queues = {key: asyncio.Queue() for key in entered}

    async def events(key, after_event_id=0):
        entered[key].set()
        while True:
            yield await queues[key].get()
            queues[key].task_done()

    client.events = events
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "satellites": {"device-a": "satellite-a", "device-b": "satellite-b"},
            "pipeline_selects": {},
        },
    )
    runtime = Runtime(hass, entry, client)
    await runtime.start()
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in entered.values())), 1
        )
        a = runtime.ledger.open_voice("satellite-a")
        b = runtime.ledger.open_voice("satellite-b")
        await queues["satellite-a"].put(
            {
                "event_id": 1,
                "event": {
                    "satellite": "satellite-a",
                    "turn_id": a.turn_id,
                    "stage": "playback_end",
                },
            }
        )
        await queues["satellite-a"].join()
        assert runtime.ledger.current("satellite-a") is None
        assert runtime.ledger.current("satellite-b") == b
    finally:
        await runtime.stop()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_real_entry_registers_pair_and_owns_pipeline(configured_hass, client):  # noqa: F811
    import asyncio

    from homeassistant.components import assist_pipeline
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    hass = configured_hass

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events
    client.bindings.return_value = [{"satellite": "satellite-a"}]
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "url": "http://server.example",
            "token": "test-only",
            "fallback_stt": "stt.contract_stt",
            "fallback_tts": "tts.contract_tts",
            "satellites": {"device-a": "satellite-a"},
            "pipeline_selects": {},
            "voices": {"device-a": "voice-two"},
        },
    )
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "satellite-a")}
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "satellites": {device.id: "satellite-a"},
            "voices": {device.id: "voice-two"},
        },
    )
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
    runtime = hass.data["assist_live"][entry.entry_id]
    stt_id = runtime.stt[device.id].entity_id
    tts_id = runtime.tts[device.id].entity_id
    assert hass.states.get(stt_id) is not None
    assert hass.states.get(tts_id) is not None
    assert er.async_get(hass).async_get(stt_id).device_id == device.id
    assert er.async_get(hass).async_get(tts_id).device_id == device.id
    saved = entry.data["pipelines"][device.id]
    pipeline = next(
        p for p in assist_pipeline.async_get_pipelines(hass) if p.id == saved
    )
    assert pipeline.stt_engine == stt_id
    assert pipeline.tts_engine == tts_id
    assert pipeline.tts_voice == "voice-two"
    assert await hass.config_entries.async_unload(entry.entry_id)
    before = {p.id for p in assist_pipeline.async_get_pipelines(hass)}
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert {p.id for p in assist_pipeline.async_get_pipelines(hass)} == before
    assert entry.data["pipelines"][device.id] == saved
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
def runtime_entry(monkeypatch):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    monkeypatch.setattr(
        "homeassistant.components.assist_pipeline.async_get_pipelines", lambda hass: []
    )

    return MockConfigEntry(
        domain="assist_live",
        data={
            "satellites": {"device-a": "satellite-a", "device-b": "satellite-b"},
            "pipeline_selects": {},
        },
    )


async def test_idle_maintenance_writes_only_changes_and_checks_health_on_schedule(
    hass, client, runtime_entry
):
    from custom_components.assist_live import Runtime

    runtime = Runtime(hass, runtime_entry, client)
    now = [100.0]
    runtime.clock = lambda: now[0]
    client.health.return_value = True
    with (
        patch.object(
            runtime.store, "async_save", wraps=runtime.store.async_save
        ) as save,
        patch(
            "homeassistant.components.assist_pipeline.async_get_pipelines",
            return_value=[],
        ),
    ):
        for instant in (100, 101, 105, 110, 129.999):
            now[0] = instant
            await runtime._maintenance(None)
        assert save.await_count == 0
        assert client.health.await_count == 1
        turn = runtime.ledger.open_voice("satellite-a")
        await runtime.ledger.connect(turn)
        assert save.await_count == 1
        now[0] = 130
        await runtime._maintenance(None)
        assert save.await_count == 1
        assert client.health.await_count == 2
        client.health.return_value = False
        for instant in (160, 164.999, 165):
            now[0] = instant
            await runtime._maintenance(None)
        assert client.health.await_count == 4


async def test_event_cursors_are_debounced_and_resume_after_restart(
    hass, client, runtime_entry
):
    import asyncio

    from custom_components.assist_live import Runtime

    entered = {key: asyncio.Event() for key in ("satellite-a", "satellite-b")}
    queues = {key: asyncio.Queue() for key in entered}
    subscriptions = []

    async def events(key, after_event_id=0):
        subscriptions.append((key, after_event_id))
        entered[key].set()
        while True:
            row = await queues[key].get()
            try:
                yield row
            finally:
                queues[key].task_done()

    client.events = events
    runtime = Runtime(hass, runtime_entry, client)
    now = [0.0]
    runtime.clock = lambda: now[0]
    await runtime.start()
    try:
        with patch.object(
            runtime.store, "async_save", wraps=runtime.store.async_save
        ) as save:
            for key, event_id in (("satellite-a", 11), ("satellite-b", 22)):
                await queues[key].put(
                    {
                        "event_id": event_id,
                        "event": {
                            "satellite": key,
                            "turn_id": None,
                            "stage": "wake_detected",
                        },
                    }
                )
                await asyncio.wait_for(queues[key].join(), 1)
            assert save.await_count == 0
            now[0] = 5
            with patch(
                "homeassistant.components.assist_pipeline.async_get_pipelines",
                return_value=[],
            ):
                await runtime._maintenance(None)
            assert save.await_count == 1
            assert save.call_args.args[0] == {
                "version": 1,
                "turns": [],
                "cursors": {"satellite-a": 11, "satellite-b": 22},
            }
    finally:
        await runtime.stop()
    for event in entered.values():
        event.clear()
    restarted = Runtime(hass, runtime_entry, client)
    await restarted.start()
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in entered.values())), 1
        )
        assert subscriptions[-2:] == [("satellite-a", 11), ("satellite-b", 22)]
    finally:
        await restarted.stop()


@pytest.mark.parametrize(
    "broken",
    [
        {"turns": [], "cursors": 5},
        {"cursors": 5},
        {"turns": []},
        {"turns": [{"turn_id": "bad"}], "cursors": {}},
        {"turns": [{"satellite": "satellite-a"}], "cursors": {}},
        {"turns": [{"satellite": "satellite-a", "turn_id": "bad"}], "cursors": {}},
        {"version": 999, "turns": [], "cursors": {}},
        pytest.param(
            {
                "turns": [
                    {
                        "satellite": "satellite-z",
                        "turn_id": "01900000-0000-7000-8000-000000000001",
                    }
                ],
                "cursors": {},
            },
            id="foreign_satellite",
        ),
    ],
)
async def test_corrupt_store_does_not_block_setup(
    hass, client, runtime_entry, broken, caplog
):
    import asyncio

    from homeassistant.helpers import issue_registry as ir

    from custom_components.assist_live import Runtime

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events
    runtime = Runtime(hass, runtime_entry, client)
    with patch.object(runtime.store, "async_load", return_value=broken):
        await runtime.start()
    try:
        assert runtime.cursors == {}
        assert client.event.await_count == 0
        assert "Dropped invalid stored turn data" in caplog.text
        assert ir.async_get(hass).async_get_issue(
            "assist_live", f"{runtime_entry.entry_id}_invalid_stored_state"
        )
        assert "bad" not in caplog.text
    finally:
        await runtime.stop()


async def test_corrupt_store_keeps_valid_entries(hass, client, runtime_entry):
    import asyncio
    from uuid import uuid7

    from custom_components.assist_live import Runtime

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events
    turn_id = str(uuid7())
    runtime = Runtime(hass, runtime_entry, client)
    saved = {
        "turns": [
            {"satellite": "satellite-a", "turn_id": turn_id},
            {"satellite": "satellite-b"},
        ],
        "cursors": {"satellite-a": 7, "satellite-b": -1, "unknown": 9},
    }
    with patch.object(runtime.store, "async_load", return_value=saved):
        await runtime.start()
    try:
        assert runtime.cursors == {"satellite-a": 7}
        assert client.event.call_args.args[0].turn_id == turn_id
        await runtime._save()
        assert await runtime.store.async_load() == {
            "version": 1,
            "turns": [],
            "cursors": {"satellite-a": 7},
        }
    finally:
        await runtime.stop()


async def test_old_turn_list_store_keeps_restart_markers(hass, client, runtime_entry):
    import asyncio
    from uuid import uuid7

    from custom_components.assist_live import Runtime

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events

    turn_id = str(uuid7())
    runtime = Runtime(hass, runtime_entry, client)
    with patch.object(
        runtime.store,
        "async_load",
        return_value=[{"turn_id": turn_id, "satellite": "satellite-a"}],
    ):
        await runtime.start()
    try:
        assert client.event.call_args.args[0].turn_id == turn_id
        assert client.event.call_args.args[0].data == {"reason": "restart"}
        assert runtime.cursors == {}
    finally:
        await runtime.stop()


async def test_start_failure_cancels_tasks_and_removes_runtime(hass, client):
    import asyncio

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime, async_setup_entry

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "url": "https://server.example",
            "token": "test-only",
            "fallback_stt": "stt.standard",
            "fallback_tts": "tts.standard",
            "satellites": {"device-a": "satellite-a"},
            "pipeline_selects": {},
        },
    )
    captured = []

    def create(*args):
        runtime = Runtime(*args)
        captured.append(runtime)
        return runtime

    try:
        with (
            patch(
                "custom_components.assist_live.AssistLiveClient", return_value=client
            ),
            patch("custom_components.assist_live.Runtime", side_effect=create),
            patch(
                "custom_components.assist_live.async_track_time_interval",
                side_effect=RuntimeError("test start failure"),
            ),
        ):
            with pytest.raises(RuntimeError, match="test start failure"):
                await async_setup_entry(hass, entry)
        assert entry.entry_id not in hass.data["assist_live"]
        assert all(task.done() for task in captured[0].tasks)
        client.close.assert_awaited_once()
    finally:
        if entry.entry_id in hass.data.get("assist_live", {}):
            hass.data["assist_live"].pop(entry.entry_id)
            await captured[0].stop()


async def test_poison_event_retries_with_repair_backoff_and_no_cursor_skip(
    hass, client, runtime_entry, caplog
):
    import asyncio
    import logging

    from homeassistant.helpers import issue_registry as ir

    from custom_components.assist_live import Runtime

    runtime = Runtime(hass, runtime_entry, client)
    reached, release, processed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    delays, subscriptions = [], []
    poison = [True]

    async def events(key, after_event_id=0):
        if key == "satellite-b":
            await asyncio.Event().wait()
            return
        subscriptions.append(after_event_id)
        yield {
            "event_id": 7,
            "event": {
                "satellite": "satellite-b" if poison[0] else key,
                "turn_id": None,
                "stage": "wake_detected",
            },
        }
        processed.set()
        await asyncio.Event().wait()

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 6:
            reached.set()
            await release.wait()

    client.events = events
    runtime._sleep = sleep
    caplog.set_level(logging.DEBUG, logger="custom_components.assist_live")
    await runtime.start()
    try:
        await asyncio.wait_for(reached.wait(), 1)
        issue_id = f"{runtime_entry.entry_id}_satellite-a_events"
        assert ir.async_get(hass).async_get_issue("assist_live", issue_id)
        assert runtime.failures == {"satellite-a": 6}
        assert runtime.cursors.get("satellite-a", 0) == 0
        assert delays == [1, 2, 4, 8, 16, 30]
        retries = [
            record
            for record in caplog.records
            if record.name == "custom_components.assist_live"
            and record.levelno == logging.DEBUG
            and "retry" in record.message.lower()
        ]
        assert len(retries) == 6
        poison[0] = False
        release.set()
        await asyncio.wait_for(processed.wait(), 1)
        assert subscriptions == [0] * 7
        assert runtime.cursors["satellite-a"] == 7
        assert runtime.failures["satellite-a"] == 0
        assert ir.async_get(hass).async_get_issue("assist_live", issue_id) is None
    finally:
        await runtime.stop()


@pytest.mark.parametrize("silent", [False, True])
async def test_store_failure_does_not_acknowledge_event(
    hass, client, runtime_entry, silent
):
    import asyncio

    from custom_components.assist_live import Runtime

    runtime = Runtime(hass, runtime_entry, client)
    reached, release, processed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    runtime.clock = lambda: 10.0
    subscriptions = []

    async def events(key, after_event_id=0):
        if key == "satellite-b":
            await asyncio.Event().wait()
            return
        subscriptions.append(after_event_id)
        yield {
            "event_id": 9,
            "event": {"satellite": key, "turn_id": None, "stage": "wake_detected"},
        }
        processed.set()
        await asyncio.Event().wait()

    async def sleep(delay):
        reached.set()
        await release.wait()

    original_save = runtime.store.async_save
    failed = [False]

    async def save(data):
        if not failed[0]:
            failed[0] = True
            if silent:
                return
            raise OSError("test store failure")
        await original_save(data)

    client.events = events
    runtime._sleep = sleep
    with patch.object(runtime.store, "async_save", side_effect=save):
        await runtime.start()
        try:
            await asyncio.wait_for(reached.wait(), 1)
            assert runtime.cursors.get("satellite-a", 0) == 0
            assert not runtime.tasks[0].done()
            release.set()
            await asyncio.wait_for(processed.wait(), 1)
            assert subscriptions == [0, 0]
            assert runtime.cursors["satellite-a"] == 9
        finally:
            await runtime.stop()


async def test_maintenance_store_failure_uses_backoff(hass, client, runtime_entry):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.assist_live import Runtime

    runtime = Runtime(hass, runtime_entry, client)
    now = [100.0]
    runtime.clock = lambda: now[0]
    runtime.ledger.open_voice("satellite-a")
    with patch.object(
        runtime.store, "async_save", side_effect=OSError("test store failure")
    ) as save:
        for instant in (100, 100.5, 101, 102, 103):
            now[0] = instant
            await runtime._maintenance(None)
        assert save.await_count == 3
        assert runtime.failures == {"satellite-a": 3, "satellite-b": 3}
        for key in ("satellite-a", "satellite-b"):
            assert ir.async_get(hass).async_get_issue(
                "assist_live", f"{runtime_entry.entry_id}_{key}_events"
            )


def test_real_store_file_failure_is_detected_and_raises_repair(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from textwrap import dedent

    # A subprocess excludes the HA pytest fixture that replaces Store I/O.
    program = dedent("""
        import asyncio
        import sys
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import issue_registry as ir
        from homeassistant.helpers.storage import Store
        from homeassistant.util.file import WriteError
        from custom_components.assist_live import Runtime

        class Client:
            async def health(self, **kwargs):
                return True
            async def close(self):
                pass

        async def main():
            hass = HomeAssistant(sys.argv[1])
            await ir.async_get(hass).async_load()
            entry = SimpleNamespace(entry_id="disk-test", data={
                "satellites": {"device-a": "satellite-a"}, "pipeline_selects": {},
            })
            runtime = Runtime(hass, entry, Client())
            assert type(runtime.store) is Store
            now = [100.0]
            runtime.clock = lambda: now[0]
            initial = {"version": 1, "turns": [], "cursors": {}}
            await runtime.store.async_save(initial)
            assert await runtime.store.async_load() == initial
            path = Path(runtime.store.path)
            before = path.read_bytes()
            runtime.ledger.open_voice("satellite-a")
            try:
                with (
                    patch("homeassistant.helpers.storage.write_utf8_file",
                          side_effect=WriteError("test file-layer failure")) as write,
                    patch("homeassistant.components.assist_pipeline.async_get_pipelines",
                          return_value=[]),
                ):
                    for instant in (100, 101, 103):
                        now[0] = instant
                        await runtime._maintenance(None)
                    assert write.call_count == 3
                    assert runtime.failures["satellite-a"] == 3
                    assert runtime._last_saved == initial
                    assert ir.async_get(hass).async_get_issue(
                        "assist_live", "disk-test_satellite-a_events")
                    assert path.read_bytes() == before
                print("PASS real Store: 3 file-layer failures detected; "
                      "unchanged disk; repair raised")
            finally:
                await runtime.stop()
                await hass.async_stop(force=True)
        asyncio.run(main())
    """)
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "PASS real Store: 3 file-layer failures detected; unchanged disk; repair raised"
        in result.stdout
    )
