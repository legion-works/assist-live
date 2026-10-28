def test_missing_native_observations_fail():
    from tools.native_matrix import validate_native_matrix

    assert validate_native_matrix([])


def observed_rows():
    from tools.native_matrix import run_native_matrix

    rows = run_native_matrix()
    for row in rows:
        row["observation"] = {
            "observer": row["observer"],
            "scope": "device",
            "passed": True,
            "detail": "Synthetic fixture only, not a device result.",
            "evidence": "fixture-observation",
            "observed_at": "2026-10-28T00:00:00Z",
        }
    return rows


def test_every_function_requires_both_server_paths(monkeypatch):
    from tools.native_matrix import CONTRACT, validate_native_matrix

    monkeypatch.delenv("ASSIST_LIVE_NATIVE_OBSERVATIONS", raising=False)
    rows = observed_rows()
    assert len(rows) == 2 * 16
    assert {row["function"] for row in rows} == set(CONTRACT)
    assert validate_native_matrix(rows) == []
    for index in range(len(rows)):
        assert validate_native_matrix(rows[:index] + rows[index + 1 :])


def test_default_runner_never_claims_unobserved_functions(monkeypatch):
    from tools.native_matrix import run_native_matrix, validate_native_matrix

    monkeypatch.delenv("ASSIST_LIVE_NATIVE_OBSERVATIONS", raising=False)
    rows = run_native_matrix()
    assert all(row["observation"] is None for row in rows)
    assert len(validate_native_matrix(rows)) == 32


def test_each_missing_or_unsatisfactory_observation_fails(monkeypatch):
    import copy

    from tools.native_matrix import validate_native_matrix

    monkeypatch.delenv("ASSIST_LIVE_NATIVE_OBSERVATIONS", raising=False)
    for value in (None, {}, {"passed": True}):
        rows = observed_rows()
        rows[0]["observation"] = value
        assert validate_native_matrix(rows)
    for field in ("detail", "evidence", "observed_at", "observer", "scope", "passed"):
        rows = observed_rows()
        row = copy.deepcopy(rows[0]["observation"])
        del row[field]
        rows[0]["observation"] = row
        assert validate_native_matrix(rows), field


def test_physical_led_and_mute_claims_cannot_be_automated(monkeypatch):
    from tools.native_matrix import validate_native_matrix

    monkeypatch.delenv("ASSIST_LIVE_NATIVE_OBSERVATIONS", raising=False)
    for function in ("leds", "mute"):
        rows = observed_rows()
        row = next(row for row in rows if row["function"] == function)
        row["observation"]["observer"] = "automated"
        assert any(
            "unsatisfactory" in failure for failure in validate_native_matrix(rows)
        )


def test_virtual_observations_never_satisfy_device_matrix(monkeypatch):
    from tools.native_matrix import validate_native_matrix

    monkeypatch.delenv("ASSIST_LIVE_NATIVE_OBSERVATIONS", raising=False)
    rows = observed_rows()
    rows[0]["observation"]["scope"] = "virtual"
    assert validate_native_matrix(rows)


def test_changed_unknown_and_duplicate_rows_fail(monkeypatch):
    from tools.native_matrix import validate_native_matrix

    monkeypatch.delenv("ASSIST_LIVE_NATIVE_OBSERVATIONS", raising=False)
    rows = observed_rows()
    assert validate_native_matrix(rows + [rows[0]])
    rows[0]["expected"] = "A relaxed result."
    assert validate_native_matrix(rows)
    rows[0]["function"] = "unknown"
    assert validate_native_matrix(rows)


def test_external_observations_and_cli_never_invent_values(
    tmp_path, monkeypatch, capsys
):
    import json

    from tools.native_matrix import main, run_native_matrix

    monkeypatch.delenv("ASSIST_LIVE_NATIVE_OBSERVATIONS", raising=False)
    source = tmp_path / "observations.json"
    source.write_text(json.dumps(observed_rows()))
    monkeypatch.setenv("ASSIST_LIVE_NATIVE_OBSERVATIONS", str(source))
    assert run_native_matrix() == observed_rows()
    output = tmp_path / "native.json"
    document = tmp_path / "matrix.md"
    document.write_text(" ".join(f"`{row['function']}`" for row in observed_rows()))
    assert main(["--matrix", str(document), "--output", str(output), "--check"]) == 0
    assert "PASS native matrix" in capsys.readouterr().out
    monkeypatch.delenv("ASSIST_LIVE_NATIVE_OBSERVATIONS")
    assert main(["--matrix", str(document), "--output", str(output), "--check"]) == 1
    assert "PASS native matrix" not in capsys.readouterr().out
    assert all(row["observation"] is None for row in json.loads(output.read_text()))


async def test_virtual_subset_refuses_another_target_before_reading_a_token(
    tmp_path, monkeypatch
):
    import pytest

    from tools import native_matrix

    monkeypatch.setattr(native_matrix, "URL", "http://example.invalid:18123")
    with pytest.raises(ValueError, match="isolated loopback"):
        await native_matrix.run_virtual(
            tmp_path / "not-read.json", tmp_path / "not-read.jsonl"
        )


def test_native_observation_time_requires_a_time_zone(monkeypatch):
    from tools.native_matrix import validate_native_matrix

    monkeypatch.delenv("ASSIST_LIVE_NATIVE_OBSERVATIONS", raising=False)
    for value in ("not-a-time", "2026-10-28T00:00:00"):
        rows = observed_rows()
        rows[0]["observation"]["observed_at"] = value
        assert validate_native_matrix(rows)
