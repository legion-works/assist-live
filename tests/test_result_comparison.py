import copy

import pytest

from tests.test_fault_runner import set_reasons, trial
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
    set_reasons(row, "after-final")
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


@pytest.mark.parametrize(
    "text,expected",
    [
        ("eleven hundred", ["1100"]),
        ("nineteen hundred", ["1900"]),
        ("twenty two hundred", ["2200"]),
        ("twenty-two hundred", ["2200"]),
        ("fifteen hundred and five", ["1505"]),
        ("ninety nine hundred ninety nine", ["9999"]),
        ("one thousand nineteen hundred", ["2900"]),
        ("twelve thousand twenty-two hundred and five", ["14205"]),
        ("ninety nine thousand fifteen hundred", ["100500"]),
        ("nineteen hundred thousand", ["1900", "thousand"]),
        ("nine hundred ninety nine thousand eleven hundred", ["999000", "1100"]),
    ],
)
def test_spoken_hundred_form_including_thousands(text, expected):
    assert words(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("one hundred and five", ["105"]),
        ("a hundred", ["100"]),
        ("twenty five", ["25"]),
        ("twenty-five", ["25"]),
        ("nine nine nine", ["9", "9", "9"]),
        ("zero", ["0"]),
        ("oh", ["oh"]),
        ("oh five", ["oh", "5"]),
        ("a thousand and one", ["1001"]),
        ("two thousand twenty six", ["2026"]),
        ("first", ["first"]),
        ("twenty first", ["twenty", "first"]),
        ("one hundredth", ["one", "hundredth"]),
        ("twenty two hundredth", ["twenty", "two", "hundredth"]),
        ("one timer", ["1", "timer"]),
        ("an hour", ["an", "hour"]),
        ("ninety nine thousand nine hundred ninety nine", ["99999"]),
        ("one hundred thousand and one", ["100001"]),
        ("eight hundred and ninety four", ["894"]),
        ("2:30", ["2", "30"]),
        ("894", ["894"]),
        ("one million", ["1", "million"]),
    ],
)
def test_reviewer_adversarial_number_cases(text, expected):
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


def test_hundred_forms_share_only_the_expected_numeric_tokens():
    for multiplier in range(1, 100):
        for remainder in range(100):
            value = multiplier * 100 + remainder
            tail = " and " + spell_number(remainder) if remainder else ""
            spoken = spell_number(multiplier) + " hundred" + tail
            command = "start timer for " + spoken + " seconds"
            canonical = "start timer for " + spell_number(value) + " seconds"
            expected = ["start", "timer", "for", str(value), "seconds"]
            assert words(command) == words(canonical) == expected
            assert words(command) != words(f"start timer for {value + 1} seconds")
            assert words(command) != words("start the timer for " + spoken + " seconds")


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
