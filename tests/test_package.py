def test_package_boundary():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / "custom_components/assist_live/manifest.json").read_text()
    )
    assert manifest["domain"] == "assist_live"
    assert manifest["config_flow"] is True
    assert not (root / "ha-app").exists()
    assert "Apache" in (root / "LICENSE").read_text()
