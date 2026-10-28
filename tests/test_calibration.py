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


def replies(count=50, start_error=100, end_error=100):
    rows = []
    for i in range(count):
        row = copy.deepcopy(sample())
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
    result = calibrate(replies(**kwargs), "voice-pe")
    assert result["proxies"][boundary]["accepted"] is accepted
    assert result["proxies"][boundary]["p90_absolute_error_ms"] == error
    assert result["proxies"][boundary]["status"] == (
        "accepted" if accepted else "rejected"
    )
    assert result["latency_gates_accepted"] is accepted
    assert result["alignment_uncertainty_ms"] == {"p50": 10, "p90": 10, "maximum": 10}
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
        "".join(json.dumps(row) + "\n" for row in replies(count, start_error=error))
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
