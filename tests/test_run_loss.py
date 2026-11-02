import pytest

from tools import measurement


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
    tmp_path, with_epoch
):
    import json

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
                return {
                    "data": {
                        "timing_events_dropped": 4,
                        **({"counters_since_utc_ns": 100} if with_epoch else {}),
                    }
                }
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
    assert result["integration"]["last_reset_utc_ns"] == (100 if with_epoch else None)
    if with_epoch:
        assert result["server"]["test_satellite"]["boot_id"] == 0
        assert result["server"]["test_satellite"]["last_reset_utc_ns"] is None
        assert result["device"]["test_satellite"]["boot_id"] == 0
    assert len(calls) == 3


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
