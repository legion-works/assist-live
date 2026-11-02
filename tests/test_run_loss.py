import json
import time
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_pipeline_contract import pipeline_hass as configured_hass  # noqa: F401

from tools import fault_runner, measurement


def snapshot(value=0, reset=1):
    return {
        "observed_utc_ns": 10,
        "integration": {"value": value, "last_reset_utc_ns": reset},
        "server": {
            "test_satellite": {"value": value, "last_reset_utc_ns": reset, "boot_id": 0}
        },
        "device": {"test_satellite": {"value": value, "last_reset_utc_ns": reset}},
    }


@pytest.mark.parametrize("kind", ["integration", "server", "device"])
@pytest.mark.parametrize(
    "case", ["increase", "decrease", "reset", "missing", "no_epoch"]
)
def test_loss_window_checks_each_counter(kind, case):
    before, after = snapshot(), snapshot()
    counter = after[kind] if kind == "integration" else after[kind]["test_satellite"]
    if case == "increase":
        counter["value"] = 1
    elif case == "decrease":
        old = before[kind] if kind == "integration" else before[kind]["test_satellite"]
        old["value"] = 3
    elif case == "reset":
        counter["last_reset_utc_ns"] = 2
        if kind == "server":
            counter["boot_id"] = 1
    elif case == "missing":
        counter["value"] = None
    else:
        counter["last_reset_utc_ns"] = None
        if kind == "server":
            counter["boot_id"] = None
    result = measurement.loss_window(before, after)
    late_arrival = kind == "server" and case == "decrease"
    assert result["complete"] is late_arrival
    assert result["loss_present"] is (case == "increase")
    assert result["loss_unavailable"] is (case == "missing")
    assert result["reset_unknown"] is (
        case in ("decrease", "reset", "no_epoch") and not late_arrival
    )
    assert result["start"] == before
    assert result["end"] == after


def test_loss_window_accepts_stable_known_counters_only():
    result = measurement.loss_window(snapshot(3), snapshot(3))
    assert result["complete"]
    assert not result["loss_present"]
    assert result["counter_deltas"] == {
        "integration": 0,
        "server": {"test_satellite": 0},
        "device": {"test_satellite": 0},
    }


def test_loss_window_records_per_counter_increases():
    before, after = snapshot(2), snapshot(2)
    after["integration"]["value"] = 9
    after["server"]["test_satellite"]["value"] = 8
    after["device"]["test_satellite"]["value"] = 5
    before["device"]["second_satellite"] = {"value": 11, "last_reset_utc_ns": 1}
    after["device"]["second_satellite"] = {"value": 15, "last_reset_utc_ns": 1}
    result = measurement.loss_window(before, after)
    assert result["loss_present"]
    assert result["counter_deltas"] == {
        "integration": 7,
        "server": {"test_satellite": 6},
        "device": {"test_satellite": 3, "second_satellite": 4},
    }


def test_loss_window_does_not_invent_delta_across_counter_reset():
    before, after = snapshot(2), snapshot(5)
    after["integration"]["last_reset_utc_ns"] = 2
    after["server"]["test_satellite"]["boot_id"] = 1
    after["device"]["test_satellite"]["value"] = None
    result = measurement.loss_window(before, after)
    assert result["counter_deltas"] == {
        "integration": None,
        "server": {"test_satellite": None},
        "device": {"test_satellite": None},
    }


def test_loss_window_does_not_drop_a_satellite_missing_at_end():
    after = snapshot()
    after["server"] = {}
    result = measurement.loss_window(snapshot(), after)
    assert result["loss_unavailable"]
    assert not result["complete"]


async def test_loss_capture_records_unavailable_without_credentials():
    result = await measurement.capture_loss(None, satellites=["test_satellite"])
    assert result["observed_utc_ns"] > 0
    assert result["integration"] == {"value": None, "last_reset_utc_ns": None}
    assert result["server"]["test_satellite"]["value"] is None
    assert result["device"]["test_satellite"]["value"] is None


@pytest.mark.parametrize("with_epoch", [False, True])
async def test_loss_capture_reads_public_sources_and_does_not_guess_reset(
    hass, client, tmp_path, with_epoch
):
    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    hass.data["assist_live"] = {entry.entry_id: runtime}
    runtime.ledger.timing_events_dropped = 4
    data = await async_get_config_entry_diagnostics(hass, entry)
    epoch = data.get("counters_since_utc_ns")
    assert type(epoch) is int and epoch > 0, "diagnostics_counter_start_missing"
    if not with_epoch:
        data.pop("counters_since_utc_ns")

    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token": "test-token"}))
    calls = []

    class Response:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            if "/diagnostics/" in self.url:
                return {"data": data}
            if self.url.endswith("/health"):
                return {
                    "healthy": True,
                    **(
                        {
                            "sequence_gaps_by_satellite": {
                                "test_satellite": {"boot_id": 0, "missing": 7}
                            },
                            "counters_since_utc_ns": 200,
                        }
                        if with_epoch
                        else {}
                    ),
                }
            return {"state": "2", "attributes": {"boot_id": 0} if with_epoch else {}}

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response(url)

    result = await measurement.capture_loss(
        Session(),
        satellites=["test_satellite"],
        ha_url="http://localhost",
        ha_token_file=token,
        integration_entry="test-entry",
        device_entities={"test_satellite": "sensor.events_dropped"},
        server_url="http://localhost:8765",
        server_token="test-server-token",
    )
    assert result["integration"]["value"] == 4
    assert result["device"]["test_satellite"]["value"] == 2
    assert result["server"]["test_satellite"]["value"] == (7 if with_epoch else None)
    assert result["integration"]["last_reset_utc_ns"] == (epoch if with_epoch else None)
    if with_epoch:
        assert result["server"]["test_satellite"]["boot_id"] == 0
        assert result["server"]["test_satellite"]["last_reset_utc_ns"] is None
        assert result["device"]["test_satellite"]["boot_id"] == 0
    assert len(calls) == 3


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("reader", ["measurement", "fault_runner"])
@pytest.mark.parametrize("case", ["stable", "reload", "missing"])
async def test_real_diagnostics_counter_lifetime(
    configured_hass,  # noqa: F811
    client,
    tmp_path,
    monkeypatch,
    reader,
    case,
):
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    hass = configured_hass
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "url": "http://server.example",
            "token": "test-only",
            "fallback_stt": "stt.contract_stt",
            "fallback_tts": "tts.contract_tts",
            "satellites": {},
        },
    )
    entry.add_to_hass(hass)
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token": "test-only"}))
    payload = {}

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            return {"data": payload}

    class Session:
        def get(self, url, **kwargs):
            assert url.endswith("/api/diagnostics/config_entry/" + entry.entry_id)
            return Response()

    @asynccontextmanager
    async def control(*args):
        async def command(name):
            if name == "assist_pipeline/pipeline/list":
                return {"pipelines": [{"id": "pipeline", "stt_engine": "stt.test"}]}
            assert name == "config/entity_registry/list"
            return [
                {
                    "entity_id": "stt.test",
                    "platform": "assist_live",
                    "config_entry_id": entry.entry_id,
                }
            ]

        yield command

    monkeypatch.setattr(fault_runner, "control_command", control)

    async def read():
        nonlocal payload
        payload = await async_get_config_entry_diagnostics(hass, entry)
        if case == "missing":
            payload.pop("counters_since_utc_ns", None)
        if reader == "measurement":
            return await measurement.capture_loss(
                Session(),
                satellites=["test_satellite"],
                ha_url=fault_runner.URL,
                ha_token_file=token,
                integration_entry=entry.entry_id,
            )
        data = await fault_runner.fallback_snapshot(Session(), token, "pipeline")
        assert data is not None, "fault_reader_keeps_real_diagnostics"
        return await measurement.capture_loss(
            None, satellites=["test_satellite"], integration_data=data
        )

    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        setup_started = time.time_ns()
        assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            runtime = hass.data["assist_live"][entry.entry_id]
            assert runtime.ledger.timing_events_dropped == 0
            assert runtime.ledger.turn_opens_dropped == 0
            setup_finished = time.time_ns()
            runtime.ledger.timing_events_dropped = 4
            before = await read()
            if case == "reload":
                assert await hass.config_entries.async_reload(entry.entry_id)
                runtime = hass.data["assist_live"][entry.entry_id]
                assert runtime.ledger.timing_events_dropped == 0
                assert runtime.ledger.turn_opens_dropped == 0
            else:
                runtime.update_options(entry)
                await runtime._maintenance(None)
            runtime.ledger.timing_events_dropped = 7
            after = await read()
            if case != "missing":
                epoch = before["integration"]["last_reset_utc_ns"]
                assert type(epoch) is int and epoch > 0, "counter_start_is_utc_ns"
                if case == "reload":
                    assert after["integration"]["last_reset_utc_ns"] != epoch, (
                        "counter_start_renewed_on_reload"
                    )
                else:
                    assert after["integration"]["last_reset_utc_ns"] == epoch, (
                        "counter_start_stable_between_reads"
                    )
                assert setup_started <= epoch <= setup_finished, (
                    "counter_start_is_setup_utc_time"
                )
            start, end = snapshot(), snapshot()
            start["integration"] = before["integration"]
            end["integration"] = after["integration"]
            window = measurement.loss_window(start, end)
            assert window["counter_deltas"]["integration"] == (
                3 if case == "stable" else None
            ), "counter_delta_requires_same_start"
            assert window["reset_unknown"] is (case != "stable"), (
                "counter_reset_or_missing_start_is_unknown"
            )
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["server", "device"])
@pytest.mark.parametrize("changed", [False, True])
def test_device_boot_id_defines_counter_reset(kind, changed):
    before, after = snapshot(), snapshot()
    before[kind]["test_satellite"].update(last_reset_utc_ns=None, boot_id=0)
    after[kind]["test_satellite"].update(
        last_reset_utc_ns=None, boot_id=1 if changed else 0
    )
    result = measurement.loss_window(before, after)
    assert result["reset_unknown"] is changed
    assert result["reset_methods"][kind + ":test_satellite"] == "device_boot_id"


def test_sequence_gap_decrease_on_same_boot_is_late_arrival_not_reset():
    before, after = snapshot(), snapshot()
    before["server"]["test_satellite"].update(value=3, boot_id=0)
    after["server"]["test_satellite"].update(value=1, boot_id=0)
    result = measurement.loss_window(before, after)
    assert result["loss_present"]
    assert not result["reset_unknown"]
    assert result["server_missing_after_settle"]["test_satellite"] == 1
    assert result["counter_deltas"]["server"]["test_satellite"] == -2


@pytest.mark.parametrize("boot", [0, 18446744073709551615])
def test_counter_keeps_unsigned_integer_boot_id(boot):
    assert measurement.counter(0, boot=boot)["boot_id"] == boot


@pytest.mark.parametrize("boot", [True, -1, "1"])
def test_counter_does_not_accept_invalid_boot_id(boot):
    assert "boot_id" not in measurement.counter(0, boot=boot)


def test_no_device_events_is_not_loss_or_device_completeness():
    before, after = snapshot(), snapshot()
    for snap in (before, after):
        snap["server"]["test_satellite"].update(boot_id=None, last_reset_utc_ns=None)
    result = measurement.loss_window(before, after)
    assert result["no_device_events"] == ["test_satellite"]
    assert not result["loss_present"]
    assert not result["complete"]


def test_first_device_boot_during_run_is_reset_unknown():
    before, after = snapshot(), snapshot()
    before["server"]["test_satellite"].update(boot_id=None, last_reset_utc_ns=None)
    result = measurement.loss_window(before, after)
    assert result["reset_unknown"]
    assert not result["complete"]


@pytest.mark.parametrize(
    "malformed", [None, [], {"sequence_gaps_by_satellite": {"test_satellite": 3}}]
)
async def test_malformed_public_counters_stay_unavailable(malformed):
    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            return malformed

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    result = await measurement.capture_loss(
        Session(),
        satellites=["test_satellite"],
        server_url="http://localhost",
        server_token="test-token",
    )
    assert result["server"]["test_satellite"]["value"] is None
