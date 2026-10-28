import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import annotate_corpus
from tools.corpus import load_corpus
from tools.device_replay import ReplayError, sample_errors


def percentile(values: list[float], percent: float) -> float:
    if (
        not values
        or not 0 <= percent <= 100
        or any(not math.isfinite(value) for value in values)
    ):
        raise ReplayError("INVALID_PERCENTILE_INPUT")
    return annotate_corpus.percentile(values, percent / 100)


def calibrate(rows: list[dict[str, Any]], device: str) -> dict[str, Any]:
    if len(rows) < 50:
        raise ReplayError("CALIBRATION_TOO_FEW_REPLIES: n>=50 required")
    seen = set()
    for row in rows:
        errors = sample_errors(row)
        if errors:
            raise ReplayError("CALIBRATION_INVALID_SAMPLE: " + ",".join(errors))
        if row["turn_id"] in seen:
            raise ReplayError("CALIBRATION_DUPLICATE_REPLY")
        seen.add(row["turn_id"])
    if (
        len({row["baseline_config_id"] for row in rows}) != 1
        or len({row["calibration_revision"] for row in rows}) != 1
    ):
        raise ReplayError("CALIBRATION_MIXED_CONFIGURATION")
    uncertainty = [row["alignment"]["uncertainty_ms"] for row in rows]
    uncertainty_p90 = percentile(uncertainty, 90)
    proxies = {}
    for boundary in ("start", "end"):
        absolute_errors = [
            abs(
                row["playback_" + boundary]["t_utc_ns"]
                - row["server_clock"]["offset_ns"]
                - row["alignment"]["reply_" + boundary + "_utc_ns"]
            )
            / 1e6
            for row in rows
        ]
        p90 = percentile(absolute_errors, 90)
        accepted = p90 + uncertainty_p90 <= 100
        proxies[boundary] = {
            "n": len(rows),
            "p50_absolute_error_ms": percentile(absolute_errors, 50),
            "p90_absolute_error_ms": p90,
            "p90_error_bound_ms": p90 + uncertainty_p90,
            "accepted": accepted,
            "status": "accepted" if accepted else "rejected",
        }
    return {
        "schema_version": 1,
        "device": device,
        "n": len(rows),
        "baseline_config_id": rows[0]["baseline_config_id"],
        "calibration_revision": rows[0]["calibration_revision"],
        "percentile_method": "linear-interpolation-(n-1)*p",
        "proxies": proxies,
        "latency_gates_accepted": all(proxy["accepted"] for proxy in proxies.values()),
        "alignment_uncertainty_ms": {
            "p50": percentile(uncertainty, 50),
            "p90": percentile(uncertainty, 90),
            "maximum": max(uncertainty),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate playback proxies from external-microphone replay rows."
    )
    parser.add_argument("--device", required=True)
    parser.add_argument("--replies", type=int, required=True)
    parser.add_argument("--external-mic", action="store_true", required=True)
    parser.add_argument(
        "--runs", type=Path, default=os.environ.get("ASSIST_LIVE_CALIBRATION_RUNS")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.replies < 50:
            raise ReplayError("CALIBRATION_TOO_FEW_REPLIES: n>=50 required")
        if args.runs is None:
            raise ReplayError(
                "CALIBRATION_INPUT_NOT_AVAILABLE: supply --runs after device replay"
            )
        rows = load_corpus(args.runs)
        if len(rows) != args.replies:
            raise ReplayError("CALIBRATION_REPLY_COUNT_MISMATCH")
        result = calibrate(rows, args.device)
        args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        for boundary, proxy in result["proxies"].items():
            print(
                f"{boundary} p50={proxy['p50_absolute_error_ms']:g}ms "
                f"p90={proxy['p90_absolute_error_ms']:g}ms {proxy['status']}"
            )
        if args.check:
            if not result["latency_gates_accepted"]:
                raise ReplayError("CALIBRATION_PROXY_REJECTED: latency gates blocked")
            print("PASS calibration n>=50")
    except (ReplayError, OSError, ValueError) as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
