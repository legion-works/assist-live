from argparse import Namespace

import pytest

from tests.test_fault_runner import trial
from tools import fault_runner


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
        0.04 if count == 5 else 0.02
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
