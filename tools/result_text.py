import re

VALUES = {
    word: value
    for value, word in enumerate(
        (
            "zero one two three four five six seven eight nine ten eleven twelve "
            "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
        ).split()
    )
}
VALUES.update(
    {
        word: value * 10
        for value, word in enumerate(
            "twenty thirty forty fifty sixty seventy eighty ninety".split(), 2
        )
    }
)
ORDINALS = set(
    (
        "first second third fourth fifth sixth seventh eighth ninth tenth eleventh "
        "twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth "
        "nineteenth twentieth thirtieth fortieth fiftieth sixtieth seventieth "
        "eightieth ninetieth hundredth thousandth"
    ).split()
)


def _under_hundred(tokens: list[str], start: int) -> tuple[int, int]:
    if start >= len(tokens) or tokens[start] not in VALUES:
        return 0, start
    value = VALUES[tokens[start]]
    end = start + 1
    if value >= 20 and end < len(tokens) and 0 < VALUES.get(tokens[end], 0) < 10:
        value += VALUES[tokens[end]]
        end += 1
    return value, end


def _under_thousand(tokens: list[str], start: int) -> tuple[int, int]:
    if (
        start + 1 < len(tokens)
        and tokens[start + 1] == "hundred"
        and (tokens[start] == "a" or 0 < VALUES.get(tokens[start], 0) < 10)
    ):
        value = (1 if tokens[start] == "a" else VALUES[tokens[start]]) * 100
        end = start + 2
        tail = end + (end < len(tokens) and tokens[end] == "and")
        remainder, final = _under_hundred(tokens, tail)
        return (value + remainder, final) if final > tail else (value, end)
    return _under_hundred(tokens, start)


def words(text: str) -> list[str]:
    tokens = re.findall(r"[^\W_]+", text.lower())
    result = []
    start = 0
    while start < len(tokens):
        value, end = _under_thousand(tokens, start)
        if tokens[start : start + 2] == ["a", "thousand"]:
            value, end = 1, start + 1
        if (
            end > start
            and value > 0
            and end < len(tokens)
            and tokens[end] == "thousand"
        ):
            value *= 1000
            end += 1
            tail = end + (end < len(tokens) and tokens[end] == "and")
            remainder, final = _under_thousand(tokens, tail)
            if final > tail:
                value += remainder
                end = final
        if end == start:
            result.append(tokens[start])
            start += 1
        elif end < len(tokens) and tokens[end] in ORDINALS:
            # Ordinal words must not become cardinal numbers.
            result.extend(tokens[start : end + 1])
            start = end + 1
        else:
            result.append(str(value))
            start = end
    return result
