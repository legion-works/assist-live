from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


@callback
def update_issues(
    hass: HomeAssistant,
    entry_id: str,
    healthy: bool,
    selected: list[str],
    owned: set[str],
) -> None:
    shared = any(selected.count(name) > 1 for name in owned)
    for kind, active in (
        ("server_unavailable", not healthy),
        ("shared_pipeline", shared),
    ):
        issue_id = f"{entry_id}_{kind}"
        if active:
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=kind,
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
