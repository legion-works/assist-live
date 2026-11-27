import pytest
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from test_pipeline_contract import pipeline_hass  # noqa: F401


async def test_cloud_speaker_is_audited_without_changing_its_event(hass, client):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime

    entry = MockConfigEntry(
        domain="assist_live", data={"satellites": {}, "local_only": True}
    )
    runtime = Runtime(hass, entry, client)
    received = []
    hass.bus.async_listen(
        "assist_live_speaker", callback(lambda event: received.append(event.data))
    )
    turn = runtime.ledger.open_voice("satellite-a")
    runtime.ledger.speaker_result(
        turn.turn_id,
        {"name": "synthetic-speaker", "distance": 1.25, "destination": "cloud"},
    )
    await hass.async_block_till_done()
    assert received[0]["destination"] == "cloud"
    assert runtime.speaker_policy_violations == 1
    assert ir.async_get(hass).async_get_issue(
        "assist_live", f"{entry.entry_id}_speaker_destination"
    )


def test_shared_pipeline_and_unavailable_server(hass):
    from custom_components.assist_live.repairs import update_issues

    update_issues(hass, "entry", False, ["shared", "shared"], {"shared"})
    registry = ir.async_get(hass)
    assert registry.async_get_issue("assist_live", "entry_server_unavailable")
    assert registry.async_get_issue("assist_live", "entry_shared_pipeline")
    update_issues(hass, "entry", True, ["one", "two"], {"one", "two"})
    assert registry.async_get_issue("assist_live", "entry_server_unavailable") is None
    assert registry.async_get_issue("assist_live", "entry_shared_pipeline") is None
    update_issues(hass, "entry", True, ["standard", "standard"], {"one"})
    assert registry.async_get_issue("assist_live", "entry_shared_pipeline") is None


@pytest.mark.parametrize("starts_healthy", [True, False])
async def test_health_logs_once_per_outage_and_recovery(
    hass, client, caplog, starts_healthy
):
    import logging

    from assist_live_client.models import HealthCapabilities
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime

    runtime = Runtime(hass, MockConfigEntry(data={"satellites": {}}), client)
    now = 0.0
    runtime.clock = lambda: now
    client.health_capabilities.side_effect = [
        HealthCapabilities(starts_healthy, True, True),
        OSError("not for the log"),
        HealthCapabilities(False, True, True),
        HealthCapabilities(True, True, True),
        HealthCapabilities(True, True, True),
        OSError("not for the log"),
    ]
    with caplog.at_level(logging.INFO, logger="custom_components.assist_live"):
        for _ in range(6):
            await runtime.refresh_health()
            now += 30
    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "custom_components.assist_live"
    ] == [
        (logging.INFO, "Assist-live server is unavailable"),
        (logging.INFO, "Assist-live server is available again"),
        (logging.INFO, "Assist-live server is unavailable"),
    ]


async def test_manually_created_forwarding_pipeline_is_checked(hass, client):
    from types import SimpleNamespace
    from unittest.mock import patch

    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime

    engine = er.async_get(hass).async_get_or_create("stt", "assist_live", "satellite-a")
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "satellites": {"device-a": "satellite-a", "device-b": "satellite-b"},
            "pipeline_selects": {
                "device-a": "select.pipeline_a",
                "device-b": "select.pipeline_b",
            },
        },
    )
    runtime = Runtime(hass, entry, client)
    hass.states.async_set("select.pipeline_a", "Manual forwarding")
    hass.states.async_set("select.pipeline_b", "Manual forwarding")
    pipeline = SimpleNamespace(
        id="manual",
        name="Manual forwarding",
        stt_engine=engine.entity_id,
        tts_engine=None,
    )
    with patch(
        "homeassistant.components.assist_pipeline.async_get_pipelines",
        return_value=[pipeline],
    ):
        await runtime._maintenance(None)
    assert ir.async_get(hass).async_get_issue(
        "assist_live", f"{entry.entry_id}_shared_pipeline"
    )


@pytest.mark.parametrize(
    ("across_entries", "selection"),
    [(True, "named"), (False, "preferred"), (True, "preferred"), (True, "unknown")],
)
async def test_repair_tracks_effective_forwarding_pipeline_sharing(
    pipeline_hass,  # noqa: F811
    client,
    across_entries,
    selection,
):
    from homeassistant.components import assist_pipeline
    from homeassistant.const import STATE_UNKNOWN
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime

    hass = pipeline_hass
    forwarding = assist_pipeline.async_get_pipeline(hass)
    ordinary = await assist_pipeline.async_create_default_pipeline(
        hass, "stt.contract_stt", "tts.contract_tts", "Ordinary pipeline"
    )
    assert ordinary is not None
    groups = (("a",), ("b",)) if across_entries else (("a", "b"),)
    entries = [
        MockConfigEntry(
            domain="assist_live",
            data={
                "satellites": {device: f"satellite-{device}" for device in devices},
                "pipeline_selects": {
                    device: f"select.pipeline_{device}" for device in devices
                },
                "pipelines": {"a": forwarding.id} if "a" in devices else {},
            },
        )
        for devices in groups
    ]
    for entry in entries:
        entry.add_to_hass(hass)
    runtimes = [Runtime(hass, entry, client) for entry in entries]
    registry = ir.async_get(hass)

    async def check(active):
        for runtime in runtimes:
            await runtime._maintenance(None)
            issue = registry.async_get_issue(
                "assist_live", f"{runtime.entry.entry_id}_shared_pipeline"
            )
            assert (issue is not None) is active

    option = {
        "named": forwarding.name,
        "preferred": assist_pipeline.OPTION_PREFERRED,
        "unknown": STATE_UNKNOWN,
    }[selection]
    hass.states.async_set("select.pipeline_a", option)
    hass.states.async_set("select.pipeline_b", option)
    await check(True)
    hass.states.async_set("select.pipeline_b", ordinary.name)
    await check(False)


async def test_unresolved_preferred_pipeline_does_not_create_a_repair(hass, client):
    from types import SimpleNamespace
    from unittest.mock import patch

    from homeassistant.components import assist_pipeline
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime

    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "satellites": {"a": "satellite-a", "b": "satellite-b"},
            "pipelines": {"a": "owned"},
            "pipeline_selects": {"a": "select.pipeline_a", "b": "select.pipeline_b"},
        },
    )
    runtime = Runtime(hass, entry, client)
    hass.states.async_set("select.pipeline_a", assist_pipeline.OPTION_PREFERRED)
    hass.states.async_set("select.pipeline_b", assist_pipeline.OPTION_PREFERRED)
    with (
        patch.object(
            assist_pipeline,
            "async_get_pipelines",
            return_value=[SimpleNamespace(id="owned", name="Forwarding")],
        ),
        patch(
            "custom_components.assist_live.routing.async_get_pipeline",
            side_effect=assist_pipeline.PipelineNotFound(
                "pipeline_not_found", "missing"
            ),
        ),
    ):
        await runtime._maintenance(None)
    assert (
        ir.async_get(hass).async_get_issue(
            "assist_live", f"{entry.entry_id}_shared_pipeline"
        )
        is None
    )
