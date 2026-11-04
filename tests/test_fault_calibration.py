import math
import statistics
from argparse import Namespace

import pytest

from tests.test_fault_runner import trial
from tools import fault_runner

RUN_STOP_SAMPLES = [
    0.7338653699844144,
    0.4402081100270152,
    0.37699276802595705,
    0.39981598599115387,
    0.41517798794666305,
    0.5386239169747569,
    1.1783444350003265,
    1.0757476069848053,
    0.456044957973063,
    0.5150751330074854,
    0.48435030702967197,
    0.5241747359978035,
    0.5367759249638766,
    0.43477140803588554,
    0.4624090220313519,
]


def test_run_samples_use_inclusive_iqr_and_allow_speech_window():
    q1, _, q3 = statistics.quantiles(RUN_STOP_SAMPLES, n=4, method="inclusive")
    margin = fault_runner.stop_margin(RUN_STOP_SAMPLES)
    assert margin == pytest.approx((q3 - q1) / 2 + 0.02), "RUN_MARGIN_INCLUSIVE_IQR"
    label = {"alignment_words": [{"start_sample": 16000, "end_sample": 21440}]}
    latency = statistics.median(RUN_STOP_SAMPLES)
    old_margin = (max(RUN_STOP_SAMPLES) - min(RUN_STOP_SAMPLES)) / 2 + 0.02
    with pytest.raises(ValueError, match="voice window too short for stop margin"):
        fault_runner.fault_sample(
            label,
            "speech",
            stop_latency_seconds=latency,
            stop_margin_seconds=old_margin,
        )
    assert fault_runner.fault_sample(
        label, "speech", stop_latency_seconds=latency, stop_margin_seconds=margin
    ) == 18720 - math.ceil(latency * 16000), "RUN_SPEECH_WINDOW_PLANS"


@pytest.mark.parametrize("samples", [[], [0.5], [0.5, 0.5], [0.5] * 5])
def test_stop_margin_keeps_twenty_millisecond_term(samples):
    assert fault_runner.stop_margin(samples) == pytest.approx(0.02), "MARGIN_FIXED_20MS"


def test_one_outlier_changes_margin_only_through_inclusive_quartiles():
    samples = [0.4, 0.45, 0.5, 0.55, 0.6]
    margin = fault_runner.stop_margin(samples)
    samples.append(10.0)
    q1, _, q3 = statistics.quantiles(samples, n=4, method="inclusive")
    changed = fault_runner.stop_margin(samples)
    assert changed == pytest.approx((q3 - q1) / 2 + 0.02), "OUTLIER_IQR_ONLY"
    assert changed - margin == pytest.approx(0.0125), "OUTLIER_MARGIN_BOUNDED"


@pytest.mark.parametrize(
    "stages,count", [("speech", 5), ("speech,pause", 5), ("pause", 1)]
)
async def test_calibration_records_five_samples_and_uses_median_spread(
    stages, count, tmp_path, monkeypatch
):
    samples = [0.48, 0.50, 0.506, 0.51, 0.52][:count]
    measured = []
    requests = []
    row = trial()
    label = {
        "id": "clip",
        "audio_sha256": "a" * 64,
        "split": "held_out",
        "set": "pause",
        "alignment_words": [{"start_sample": 16000, "end_sample": 21440}],
        "pause_start_sample": 22000,
        "pause_end_sample": 30000,
    }
    monkeypatch.setattr(fault_runner, "load_corpus", lambda _: [label])
    monkeypatch.setattr(fault_runner, "audio_input", lambda *_: b"synthetic")

    async def restore(*_):
        pass

    async def measure(*_):
        assert len(measured) < count, "CALIBRATION_STOPS_BOUNDED"
        measured.append(samples[len(measured)])
        return measured[-1]

    async def engines(*_):
        return row["stt_engines"]

    async def baselines(*_, **__):
        return row["baselines"]

    async def pipeline(*args, **kwargs):
        requests.append(kwargs)
        window = fault_runner.fault_window(args[3], args[5])
        return {
            **row,
            "stage": args[5],
            "label": args[3],
            "fault_stopped_audio_sample": sum(window) // 2,
            "acceptable": True,
            "failures": [],
        }

    monkeypatch.setattr(fault_runner.TestServer, "restore", restore)
    monkeypatch.setattr(fault_runner.TestServer, "measure_stop_latency", measure)
    monkeypatch.setattr(fault_runner, "baseline_engines", engines)
    monkeypatch.setattr(fault_runner, "collect_baselines", baselines)
    monkeypatch.setattr(fault_runner, "pipeline_trial", pipeline)
    result = await fault_runner.run(
        Namespace(
            output=tmp_path / "result.json",
            simulate=True,
            check=True,
            labels=tmp_path / "labels.jsonl",
            native_matrix=None,
            stages=stages,
            each=2,
            audio_root=tmp_path,
            token_file=tmp_path / "authentication.json",
            temporary_agent_pipeline=False,
            pipeline="existing",
        )
    )
    assert measured == samples, "CALIBRATION_SAMPLE_COUNT"
    assert result["stop_latency_samples_seconds"] == samples, "ALL_SAMPLES_RECORDED"
    assert result["stop_latency_seconds"] == (0.506 if count == 5 else 0.48)
    assert result["stop_margin_seconds"] == pytest.approx(
        0.025 if count == 5 else 0.02
    ), "MARGIN_FROM_MEASURED_SPREAD"
    assert all(
        row["stop_latency_samples_seconds"] == samples for row in result["trials"]
    )
    assert all(
        request["stop_latency_seconds"] == result["stop_latency_seconds"]
        and request["stop_margin_seconds"] == result["stop_margin_seconds"]
        for request in requests
    ), "CALIBRATION_REACHES_EVERY_TRIAL"


@pytest.mark.parametrize(
    "start,end,latency,margin,expected",
    [
        (16000, 21440, 0.506, 0.04, 10624),
        (16000, 21440, 0.1 + 0.2, 0.04, 13920),
        (16000, 17280, 0.506, 0.04, 8544),
        (16000, 17279, 0.506, 0.04, "margin"),
        (16000, 16640, 0.506, 0.02, 8224),
        (16000, 16639, 0.506, 0.02, "margin"),
        (0, 640, 0.506, 0.02, "before audio"),
    ],
)
def test_speech_margin_uses_spread_not_latency_size(
    start, end, latency, margin, expected
):
    label = {"alignment_words": [{"start_sample": start, "end_sample": end}]}
    if isinstance(expected, str):
        with pytest.raises(ValueError, match=expected):
            fault_runner.fault_sample(
                label,
                "speech",
                stop_latency_seconds=latency,
                stop_margin_seconds=margin,
            )
    else:
        assert (
            fault_runner.fault_sample(
                label,
                "speech",
                stop_latency_seconds=latency,
                stop_margin_seconds=margin,
            )
            == expected
        ), "SPREAD_MARGIN_ALLOWS_LONG_LATENCY"


async def test_calibration_each_sample_stops_then_restores_health(monkeypatch):
    server = fault_runner.TestServer()
    calls = []
    clock = 10.0

    async def docker(*args):
        nonlocal clock
        calls.append(args)
        if args[0] == "stop":
            clock += 0.506
        return '{"Running":true,"Health":{"Status":"healthy"}}'

    monkeypatch.setattr(server, "docker", docker)
    monkeypatch.setattr(fault_runner.time, "monotonic", lambda: clock)
    for _ in range(5):
        assert await server.measure_stop_latency() == pytest.approx(0.506)
    assert [args[0] for args in calls] == ["stop", "start", "inspect"] * 5
