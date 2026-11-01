import copy

import pytest

from tests.test_fault_runner import trial
from tests.test_report import build, dataset
from tools.fault_runner import acceptable
from tools.report import word_errors, words


@pytest.fixture(params=[123, 894])
def smoke_turn(request):
    row = trial()
    seconds = request.param
    row.update(
        stage="after-final",
        recognized_transcript=(
            "Start timer for two minutes three seconds"
            if seconds == 123
            else "Start the timer for eight hundred ninety four seconds."
        ),
        label={
            "transcript": (
                "start timer for 2 minutes 3 seconds"
                if seconds == 123
                else "start the timer for 894 seconds"
            ),
            "intent": {
                "name": "HassStartTimer",
                "slots": {
                    "hours": 0,
                    "minutes": 2 if seconds == 123 else 0,
                    "seconds": 3 if seconds == 123 else 894,
                },
            },
        },
        evaluated_intent={
            "name": "HassStartTimer",
            "slots": {
                "seconds": 3 if seconds == 123 else 894,
                **({"minutes": 2} if seconds == 123 else {}),
                "preferred_area_id": "virtual_test_area",
            },
        },
    )
    row["action_trace"] = [
        {
            "service": "timer.started",
            "virtual_target": "assist_live_virtual",
            "arguments": {"seconds": seconds},
            "monotonic_ns": 1,
        }
    ]
    return row


def test_smoke_numbers_context_and_missing_zero_slots_are_accepted(smoke_turn):
    assert acceptable(smoke_turn)


def test_p1_accuracy_uses_smoke_comparison(smoke_turn):
    labels, rows = dataset(200, "p1")
    baseline_labels, baseline_rows = dataset(200)
    baseline = build(baseline_rows, baseline_labels)
    labels[0].update(smoke_turn["label"])
    rows[0].update(
        {
            key: smoke_turn[key]
            for key in ("recognized_transcript", "evaluated_intent", "action_trace")
        }
    )
    result = build(rows, labels, baseline=baseline, faults={})
    assert result["accuracy"]["count"] == 200
    assert result["wrong_actions"]["count"] == 0


def test_article_and_different_number_remain_wrong(smoke_turn):
    row = copy.deepcopy(smoke_turn)
    row["recognized_transcript"] = (
        row["recognized_transcript"]
        .replace("Start the timer", "Start a timer")
        .replace("Start timer", "Start the timer")
    )
    assert not acceptable(row)
    row = copy.deepcopy(smoke_turn)
    row["recognized_transcript"] = (
        row["recognized_transcript"]
        .replace("two minutes", "three minutes")
        .replace("ninety four", "ninety five")
    )
    assert not acceptable(row)


def test_wer_normalizes_numbers_but_not_articles():
    assert word_errors("2 minutes", "two minutes") == (0, 2)
    assert word_errors("a timer", "the timer") == (1, 2)
    assert word_errors("2 minutes", "three minutes") == (1, 2)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("twenty-five", ["25"]),
        ("a hundred and five", ["105"]),
        ("eight hundred ninety four", ["894"]),
        ("one thousand and one", ["1001"]),
        ("A timer and two lights!", ["a", "timer", "and", "2", "lights"]),
        ("zero, nine; nineteen? twenty!", ["0", "9", "19", "20"]),
        ("first twenty-first 21st", ["first", "twenty", "first", "21st"]),
        ("one and two", ["1", "and", "2"]),
    ],
)
def test_text_number_grammar(text, expected):
    assert words(text) == expected


def spell_number(number):
    small = (
        "zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
    ).split()
    tens = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()
    if number < 20:
        return small[number]
    if number < 100:
        return tens[number // 10] + ("-" + small[number % 10] if number % 10 else "")
    if number < 1000:
        return (
            small[number // 100]
            + " hundred"
            + (" and " + spell_number(number % 100) if number % 100 else "")
        )
    return (
        spell_number(number // 1000)
        + " thousand"
        + (" " + spell_number(number % 1000) if number % 1000 else "")
    )


def test_all_cardinals_zero_through_999999():
    for number in range(1_000_000):
        assert words(spell_number(number)) == [str(number)], number


@pytest.mark.parametrize(
    "slots,expected,correct",
    [
        ({"minutes": 2}, {"minutes": 2, "hours": 0, "seconds": 0}, True),
        (
            {"minutes": 2, "preferred_area_id": "area", "preferred_floor_id": "floor"},
            {"minutes": 2, "preferred_area_id": "other", "preferred_floor_id": "other"},
            True,
        ),
        ({"minutes": 3}, {"minutes": 2}, False),
        ({}, {"seconds": 1}, False),
        ({}, {"name": "test light"}, False),
        ({}, {"name": None}, False),
        ({"name": "other light"}, {"name": "test light"}, False),
    ],
)
def test_intent_owner_compares_label_slots_only(slots, expected, correct):
    from tools.test_environment import intent_matches

    assert (
        intent_matches(
            {"name": "intent", "slots": slots}, {"name": "intent", "slots": expected}
        )
        is correct
    )
    assert not intent_matches(
        {"name": "other", "slots": slots}, {"name": "intent", "slots": expected}
    )


def test_wrong_operation_comparison_stays_exact(smoke_turn):
    from tools.report import operation_matches

    assert operation_matches(smoke_turn, smoke_turn["label"]) is True
    smoke_turn["action_trace"][0]["arguments"]["seconds"] += 1
    assert operation_matches(smoke_turn, smoke_turn["label"]) is False
