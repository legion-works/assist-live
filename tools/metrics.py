from typing import Any

from tools.corpus import clopper_pearson
from tools.device_replay import ReplayError


def rate(successes: int, samples: int) -> dict[str, Any]:
    if type(successes) is not int or type(samples) is not int:
        raise ReplayError("INVALID_RATE_COUNTS")
    if samples == 0 and successes == 0:
        return {"count": 0, "denominator": 0, "rate": None, "ci95": None}
    low, high = clopper_pearson(successes, samples)
    return {
        "count": successes,
        "denominator": samples,
        "rate": successes / samples,
        "ci95": [low, high],
    }
