import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_device_replay import sample
from tools.calibrate_playback import calibrate, percentile
from tools.device_replay import ReplayError

ROOT = Path(__file__).resolve().parents[1]


def replies(count=50, start_error=100, end_error=100, uncertainty=10):
    rows = []
    for i in range(count):
        row = copy.deepcopy(sample())
        row["alignment"]["uncertainty_ms"] = uncertainty
        row["turn_id"] = f"01900000-0000-7000-8000-{i:012x}"
        for event in row["events"]:
            event["turn_id"] = row["turn_id"]
        row["alignment"]["reply_start_utc_ns"] -= start_error * 1_000_000
        row["alignment"]["reply_end_utc_ns"] += end_error * 1_000_000
        rows.append(row)
    return rows


def test_linear_percentile():
    from tools.calibrate_playback import percentile

    assert percentile([0, 10, 20, 30], 50) == 15
    assert percentile([0, 10, 20, 30], 90) == 27


def test_percentile_sorts_and_handles_endpoints():
    assert percentile([30, 0, 20, 10], 25) == 7.5
    assert percentile([30, 0, 20, 10], 0) == 0
    assert percentile([30, 0, 20, 10], 100) == 30
    assert percentile([7], 90) == 7


@pytest.mark.parametrize(
    "values,percent", [([], 50), ([float("nan")], 50), ([1], -1), ([1], 101)]
)
def test_invalid_percentile_input(values, percent):
    with pytest.raises(ReplayError, match="INVALID_PERCENTILE_INPUT"):
        percentile(values, percent)


@pytest.mark.parametrize("boundary", ["start", "end"])
@pytest.mark.parametrize("error,accepted", [(100, True), (101, False)])
def test_calibration_boundary_100_accepted_101_rejected(boundary, error, accepted):
    kwargs = {boundary + "_error": error}
    result = calibrate(replies(**kwargs, uncertainty=0), "voice-pe")
    assert result["proxies"][boundary]["accepted"] is accepted
    assert result["proxies"][boundary]["p90_absolute_error_ms"] == error
    assert result["proxies"][boundary]["status"] == (
        "accepted" if accepted else "rejected"
    )
    assert result["latency_gates_accepted"] is accepted
    assert result["alignment_uncertainty_ms"] == {"p50": 0, "p90": 0, "maximum": 0}
    assert result["percentile_method"] == "linear-interpolation-(n-1)*p"


def test_n49_refused():
    with pytest.raises(ReplayError, match="CALIBRATION_TOO_FEW_REPLIES"):
        calibrate(replies(49), "voice-pe")


@pytest.mark.parametrize(
    "case,error",
    [
        ("duplicate", "CALIBRATION_DUPLICATE_REPLY"),
        ("invalid", "CALIBRATION_INVALID_SAMPLE"),
        ("mixed", "CALIBRATION_MIXED_CONFIGURATION"),
    ],
)
def test_calibration_refuses_bad_samples(case, error):
    rows = replies()
    if case == "duplicate":
        rows[-1] = rows[0]
    elif case == "invalid":
        del rows[-1]["alignment"]["uncertainty_ms"]
    else:
        rows[-1]["baseline_config_id"] = "other"
    with pytest.raises(ReplayError, match=error):
        calibrate(rows, "voice-pe")


@pytest.mark.parametrize(
    "count,error,expected",
    [
        (49, 100, "CALIBRATION_TOO_FEW_REPLIES"),
        (50, 101, "CALIBRATION_PROXY_REJECTED"),
        (50, 100, "PASS calibration n>=50"),
    ],
)
def test_check_cli(tmp_path, count, error, expected):
    runs, output = tmp_path / "runs.jsonl", tmp_path / "calibration.json"
    runs.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in replies(count, start_error=error, uncertainty=0)
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/calibrate_playback.py"),
            "--device",
            "voice-pe",
            "--replies",
            str(count),
            "--external-mic",
            "--runs",
            str(runs),
            "--output",
            str(output),
            "--check",
        ],
        capture_output=True,
        text=True,
    )
    assert expected in result.stdout + result.stderr
    assert result.returncode == (0 if count >= 50 and error <= 100 else 1)
    if count < 50:
        assert "PASS calibration n>=50" not in result.stdout
        assert not output.exists()
    else:
        assert json.loads(output.read_text())["n"] == count


def test_calibration_uncertainty_must_fit_error_slack():
    rows = replies(start_error=95, end_error=95)
    result = calibrate(rows, "voice-pe")
    assert not result["proxies"]["start"]["accepted"]
    assert not result["latency_gates_accepted"]
    for row in rows:
        row["alignment"]["uncertainty_ms"] = 5
    assert calibrate(rows, "voice-pe")["latency_gates_accepted"]


@pytest.mark.parametrize("offset_ns", [300_000_000, -300_000_000])
def test_calibration_subtracts_measured_server_offset(offset_ns):
    from tests.test_device_replay import measured_clock

    rows = replies(start_error=50, end_error=50)
    for row in rows:
        row["server_clock"] = measured_clock(offset_ns, 7)
        row["alignment"]["clock_uncertainty_ms"] = 7
        row["alignment"]["uncertainty_ms"] += 7
        for item in row["events"]:
            item["t_utc_ns"] += offset_ns
    result = calibrate(rows, "voice-pe")
    assert result["proxies"]["start"]["p90_absolute_error_ms"] == 50
    assert result["proxies"]["end"]["p90_absolute_error_ms"] == 50
    assert result["alignment_uncertainty_ms"]["p90"] == 17


def test_percentile_calls_existing_shared_helper(monkeypatch):
    import tools.annotate_corpus

    calls = []
    original = tools.annotate_corpus.percentile

    def shared(values, fraction):
        calls.append(fraction)
        return original(values, fraction)

    monkeypatch.setattr(tools.annotate_corpus, "percentile", shared)
    assert percentile([0, 10, 20, 30], 90) == 27
    assert calls == [0.9]


def test_rejected_proxy_does_not_print_pass(tmp_path, capsys):
    from tools.calibrate_playback import main

    runs = tmp_path / "runs.jsonl"
    runs.write_text("".join(json.dumps(row) + "\n" for row in replies(start_error=101)))
    code = main(
        [
            "--device",
            "voice-pe",
            "--replies",
            "50",
            "--external-mic",
            "--runs",
            str(runs),
            "--output",
            str(tmp_path / "out.json"),
            "--check",
        ]
    )
    output = capsys.readouterr()
    assert code == 1
    assert "CALIBRATION_PROXY_REJECTED" in output.err
    assert "PASS calibration n>=50" not in output.out


def test_tied_fifty_reply_percentiles_and_fractional_boundary():
    assert percentile([40] * 44 + [100] * 6, 50) == 40
    assert percentile([40] * 44 + [100] * 6, 90) == 100
    rows = replies(start_error=100.001)
    for row in rows:
        row["alignment"]["uncertainty_ms"] = 0
    assert not calibrate(rows, "voice-pe")["proxies"]["start"]["accepted"]
