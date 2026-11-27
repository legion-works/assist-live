from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

# Hassfest does not examine this file for a custom integration. This list
# follows the rules page of the HA quality scale (54 rules).
RULES = {
    "bronze": [
        "action-setup",
        "appropriate-polling",
        "brands",
        "common-modules",
        "config-flow",
        "config-flow-test-coverage",
        "dependency-transparency",
        "docs-actions",
        "docs-conditions",
        "docs-high-level-description",
        "docs-installation-instructions",
        "docs-removal-instructions",
        "docs-triggers",
        "entity-event-setup",
        "entity-unique-id",
        "has-entity-name",
        "runtime-data",
        "test-before-configure",
        "test-before-setup",
        "unique-config-entry",
    ],
    "silver": [
        "action-exceptions",
        "config-entry-unloading",
        "docs-configuration-parameters",
        "docs-installation-parameters",
        "entity-unavailable",
        "integration-owner",
        "log-when-unavailable",
        "parallel-updates",
        "reauthentication-flow",
        "test-coverage",
    ],
    "gold": [
        "devices",
        "diagnostics",
        "discovery",
        "discovery-update-info",
        "docs-data-update",
        "docs-examples",
        "docs-known-limitations",
        "docs-supported-devices",
        "docs-supported-functions",
        "docs-troubleshooting",
        "docs-use-cases",
        "dynamic-devices",
        "entity-category",
        "entity-device-class",
        "entity-disabled-by-default",
        "entity-translations",
        "exception-translations",
        "icon-translations",
        "reconfiguration-flow",
        "repair-issues",
        "stale-devices",
    ],
    "platinum": ["async-dependency", "inject-websession", "strict-typing"],
}


def test_quality_scale_lists_each_rule_with_a_status_and_comment():
    path = ROOT / "custom_components/assist_live/quality_scale.yaml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))["rules"]
    expected = [rule for tier in RULES.values() for rule in tier]
    assert len(expected) == 54
    assert list(rules) == expected
    for name, rule in rules.items():
        assert rule["status"] in {"done", "todo", "exempt"}, name
        assert isinstance(rule["comment"], str) and rule["comment"].strip(), name
    dependency = rules["dependency-transparency"]
    assert dependency["status"] == "done"
    for detail in ("Apache-2.0", "PyPI", "client-v0.1.0a1", "client-v0.1.0a2"):
        assert detail in dependency["comment"]
