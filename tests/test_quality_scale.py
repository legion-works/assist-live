import json
import re
from pathlib import Path

import pytest
import yaml
from homeassistant.components.automation.config import async_validate_config_item
from test_design_links import heading_slugs

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
    assert rules["brands"]["status"] == "exempt"
    dependency = rules["dependency-transparency"]
    assert dependency["status"] == "done"
    for detail in ("Apache-2.0", "PyPI", "client-v0.1.0a1", "client-v0.1.0a2"):
        assert detail in dependency["comment"]
    for name in (
        "docs-configuration-parameters",
        "docs-installation-parameters",
        "entity-unavailable",
        "integration-owner",
        "log-when-unavailable",
        "test-coverage",
        "devices",
        "entity-category",
        "entity-disabled-by-default",
        "exception-translations",
        "reconfiguration-flow",
        "repair-issues",
        "stale-devices",
        "inject-websession",
    ):
        assert rules[name]["status"] == "done", name
    for name in (
        "discovery",
        "discovery-update-info",
        "dynamic-devices",
        "icon-translations",
    ):
        assert rules[name]["status"] == "exempt", name
    assert rules["strict-typing"]["status"] == "todo"


def test_manifest_has_an_integration_owner():
    path = ROOT / "custom_components/assist_live/manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["codeowners"] == ["@iceteaSA"]


def test_shipped_translation_has_reconfigure_success_message():
    component = ROOT / "custom_components/assist_live"
    source = json.loads((component / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (component / "translations/en.json").read_text(encoding="utf-8")
    )
    assert english == source
    assert english["config"]["abort"]["reconfigure_successful"] == (
        "The server URL is changed."
    )


def test_setup_guide_quotes_the_shipped_binding_error():
    path = ROOT / "custom_components/assist_live/translations/en.json"
    message = json.loads(path.read_text(encoding="utf-8"))["config"]["error"][
        "binding_used"
    ]
    setup = (ROOT / "docs/setup.md").read_text(encoding="utf-8")
    quotes = {quote.removesuffix(".") for quote in re.findall(r"`([^`\n]+)`", setup)}
    assert message.removesuffix(".") in quotes, "Setup must quote the binding error"


@pytest.mark.parametrize(
    "name", [rule for rule in RULES["gold"] if rule.startswith("docs-")]
)
def test_gold_docs_rule_has_reachable_evidence(name):
    path = ROOT / "custom_components/assist_live/quality_scale.yaml"
    rule = yaml.safe_load(path.read_text(encoding="utf-8"))["rules"][name]
    assert rule["status"] == "done", name
    match = re.search(r"(docs/[\w-]+\.md)#([\w-]+)", rule["comment"])
    assert match is not None, name
    page, anchor = match.groups()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(rf"\]\({re.escape(page)}(?:#[\w-]+)?\)", readme), page
    assert anchor in heading_slugs((ROOT / page).read_text(encoding="utf-8")), (
        f"{page}#{anchor}"
    )


async def test_documented_examples_validate_as_ha_automations(hass):
    page = ROOT / "docs/usage.md"
    assert page.is_file(), "Missing user guide with automation examples"
    examples = re.findall(
        r"^```yaml\n(.*?)^```$", page.read_text(encoding="utf-8"), re.M | re.S
    )
    assert len(examples) >= 2
    for index, example in enumerate(examples):
        assert await async_validate_config_item(
            hass, str(index), yaml.safe_load(example)
        )


@pytest.mark.parametrize(
    ("flow", "document"), [("config", "setup.md"), ("options", "configuration.md")]
)
def test_field_reference_lists_each_form_label_with_a_meaning_and_default(
    flow, document
):
    path = ROOT / "custom_components/assist_live/strings.json"
    steps = json.loads(path.read_text(encoding="utf-8"))[flow]["step"]
    expected = {
        label for step in steps.values() for label in step.get("data", {}).values()
    }
    text = (ROOT / "docs" / document).read_text(encoding="utf-8")
    documented = set()
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if line.startswith("| `") and len(cells) == 3 and all(cells):
            documented.add(cells[0].strip("`"))
    assert documented >= expected
