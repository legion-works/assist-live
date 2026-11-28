from unittest.mock import AsyncMock, patch

import pytest
from test_pipeline_contract import pipeline_hass as configured_hass  # noqa: F401


async def test_flow_refuses_fallback_loop(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    with patch(
        "custom_components.assist_live.config_flow.is_forwarder", return_value=True
    ):
        result = await flow.async_step_user(
            {
                "url": "http://server.example",
                "token": "test-only",
                "fallback_stt": "stt.forward",
                "fallback_tts": "tts.standard",
            }
        )
    assert result["errors"] == {
        "fallback_stt": "fallback_loop",
        "fallback_tts": "fallback_loop",
    }


async def test_provisioned_unique_device_bindings(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.bindings = {"satellite-a", "satellite-b"}
    flow.settings = {}
    with patch(
        "custom_components.assist_live.config_flow.valid_satellite", return_value=True
    ):
        result = await flow.async_step_satellite(
            {
                "device": "device-a",
                "satellite": "satellite-a",
                "pipeline_select": "select.pipeline_a",
                "add_another": True,
            }
        )
        assert result["step_id"] == "satellite"
        result = await flow.async_step_satellite(
            {
                "device": "device-b",
                "satellite": "satellite-a",
                "pipeline_select": "select.pipeline_b",
                "add_another": True,
            }
        )
        assert result["errors"] == {"base": "binding_used"}
        result = await flow.async_step_satellite(
            {
                "device": "device-b",
                "satellite": "unknown",
                "pipeline_select": "select.pipeline_b",
                "add_another": True,
            }
        )
        assert result["errors"] == {"base": "invalid_satellite"}


async def test_user_validates_server_and_advances(hass, available_fallbacks):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    client = AsyncMock()
    client.bindings.return_value = [{"satellite": "satellite-a"}]
    with (
        patch(
            "custom_components.assist_live.config_flow.is_forwarder", return_value=False
        ),
        patch(
            "custom_components.assist_live.config_flow.AssistLiveClient",
            return_value=client,
        ) as create_client,
    ):
        result = await flow.async_step_user(
            {
                "url": "https://server.example",
                "token": "test-only",
                "fallback_stt": "stt.standard",
                "fallback_tts": "tts.standard",
            }
        )
    assert result["step_id"] == "satellite"
    assert flow.bindings == {"satellite-a"}
    assert create_client.call_args.kwargs.get("session") is async_get_clientsession(
        hass
    )
    client.close.assert_awaited_once()


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("match", "reconfigure_successful"),
        ("same_url", "reconfigure_successful"),
        ("same_endpoint", "reconfigure_successful"),
        ("wrong_server", "wrong_server"),
        ("no_configured_keys", "wrong_server"),
        ("unreachable", "cannot_connect"),
        ("unauthorized", "invalid_auth"),
        ("invalid_url", "invalid_url"),
        ("http", "reconfigure_successful"),
        ("duplicate", "already_configured"),
        ("concurrent_duplicate", "already_configured"),
        ("token_changed", "server_changed"),
    ],
)
async def test_reconfigure_updates_endpoint_after_verifying_bindings(
    configured_hass,  # noqa: F811
    case,
    reason,
):
    from assist_live_client.protocol import ProtocolError
    from homeassistant.config_entries import SOURCE_RECONFIGURE
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    hass = configured_hass
    old_url = "https://server.example"
    url = {
        "same_url": old_url,
        "same_endpoint": "https://SERVER.example:443/",
        "invalid_url": "https://server.example/?bad=true",
        "http": "http://new.example/",
    }.get(case, "https://new.example/")
    data = {
        "url": old_url,
        "token": "test-only",
        "satellites": {}
        if case == "no_configured_keys"
        else {"device-a": "satellite-a", "device-c": "satellite-c"},
        "pipelines": {"device-a": "pipeline-a"},
    }
    entry = MockConfigEntry(
        domain="assist_live", data=data, options={"local_only": True}
    )
    entry.add_to_hass(hass)

    def add_duplicate():
        MockConfigEntry(
            domain="assist_live", data={**data, "url": "https://NEW.example:443"}
        ).add_to_hass(hass)

    if case == "duplicate":
        add_duplicate()

    new_client = AsyncMock()
    new_client.bindings.return_value = [
        {"satellite": "satellite-b"},
        {"satellite": "satellite-c"},
        {"satellite": "satellite-a"},
    ]
    if case == "wrong_server":
        new_client.bindings.return_value = [{"satellite": "satellite-a"}]
    elif case == "unreachable":
        new_client.bindings.side_effect = ProtocolError("unreachable")
    elif case == "unauthorized":
        new_client.bindings.side_effect = ProtocolError(
            "request refused", kind="unauthorized"
        )
    elif case == "concurrent_duplicate":

        async def competing_entry():
            add_duplicate()
            return new_client.bindings.return_value

        new_client.bindings.side_effect = competing_entry
    elif case == "token_changed":

        async def change_token():
            hass.config_entries.async_update_entry(
                entry, data={**data, "token": "replacement"}
            )
            return new_client.bindings.return_value

        new_client.bindings.side_effect = change_token

    def create_client(target, token, *, session):
        assert target == url
        assert token == data["token"]
        assert session is async_get_clientsession(hass)
        return new_client

    with (
        patch(
            "custom_components.assist_live.config_flow.AssistLiveClient",
            side_effect=create_client,
        ) as create,
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        flows = hass.config_entries.flow
        form = await flows.async_init(
            "assist_live",
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        assert form["step_id"] == "reconfigure"
        [field] = form["data_schema"].schema
        assert field.description["suggested_value"] == old_url
        result = await flows.async_configure(form["flow_id"], {"url": url})
        if case == "http":
            assert result["step_id"] == "http_warning"
            create.assert_not_called()
            result = await flows.async_configure(form["flow_id"], {})
        if reason == "reconfigure_successful":
            assert result["reason"] == reason
            assert result.get("translation_domain") in (None, "assist_live")
            expected = {**data, "url": url.rstrip("/")}
            if case in {"match", "http"}:
                expected["endpoint_generation"] = 1
            assert entry.data == expected
            assert entry.options == {"local_only": True}
            if case == "same_url":
                reload.assert_not_called()
            else:
                reload.assert_called_once_with(entry.entry_id)
        else:
            assert (result.get("errors") or {"base": result["reason"]}) == {
                "base": reason
            }
            assert entry.data == (
                {**data, "token": "replacement"} if case == "token_changed" else data
            )
            reload.assert_not_called()
            if case == "wrong_server":
                new_client.bindings.return_value.append({"satellite": "satellite-c"})
                result = await flows.async_configure(form["flow_id"], {"url": url})
                assert result["reason"] == "reconfigure_successful"
                assert entry.data["url"] == url.rstrip("/")
    if case == "wrong_server":
        assert new_client.close.await_count == 2
    elif case not in {"duplicate", "invalid_url"}:
        new_client.close.assert_awaited_once()


async def test_user_refuses_stt_when_the_component_is_not_ready(
    hass, available_fallbacks
):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    with patch(
        "homeassistant.components.stt.async_get_speech_to_text_entity",
        side_effect=KeyError("stt component"),
    ):
        result = await flow.async_step_user(
            {
                "url": "https://server.example",
                "token": "test-only",
                "fallback_stt": "stt.standard",
                "fallback_tts": "tts.standard",
            }
        )
    assert result["step_id"] == "user"
    assert result["errors"] == {"fallback_stt": "invalid_fallback_entity"}


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ("https://server.example", "https://server.example/", True),
        ("https://server.example", "HTTPS://SERVER.Example", True),
        ("https://server.example", "https://server.example:443", True),
        ("http://server.example", "http://server.example:80/", True),
        ("http://[2001:db8::1]", "http://[2001:DB8::1]:80/", True),
        ("https://server.example/API", "https://server.example/api", False),
        ("https://server.example", "https://server.example:80", False),
        ("http://server.example", "http://server.example:8080", False),
        ("https://server.example/API", "https://server.example/API//.", False),
    ],
)
def test_request_key_is_equal_only_for_the_same_client_request(first, second, same):
    from assist_live_client import AssistLiveClient
    from yarl import URL

    from custom_components.assist_live.config_flow import request_key

    def request(url):
        return URL(AssistLiveClient(url, "test-only").url + "/v1/bindings")

    def target(url):
        sent = request(url)
        return (sent.scheme, sent.host, sent.port, sent.raw_path)

    assert (request_key(first) == request_key(second)) is same
    assert (target(first) == target(second)) is same, "the client agrees"
    assert request_key("not-a-server/") is None


@pytest.mark.parametrize(
    "submitted",
    [
        "https://server.example/v1/bindings?value",
        "https://server.example/v1/bindings#part",
        "https://user@server.example",
        "ftp://server.example",
    ],
)
def test_request_key_refuses_urls_the_client_refuses(submitted):
    from custom_components.assist_live.config_flow import request_key

    assert request_key("https://server.example") is not None
    assert request_key(submitted) is None


def test_request_key_accepts_a_malformed_international_host():
    from custom_components.assist_live.config_flow import request_key

    key = request_key("https://xn--a.example")
    assert key is not None
    assert key[1] == "xn--a.example"


async def test_stored_url_gives_the_validated_request(hass, available_fallbacks):
    from assist_live_client import AssistLiveClient

    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    submitted = "https://server.example/API//."
    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.handler = "assist_live"
    client = AsyncMock()
    client.bindings.return_value = [{"satellite": "satellite-a"}]
    with (
        patch(
            "custom_components.assist_live.config_flow.is_forwarder", return_value=False
        ),
        patch(
            "custom_components.assist_live.config_flow.AssistLiveClient",
            return_value=client,
        ) as validated,
        patch(
            "custom_components.assist_live.config_flow.valid_satellite",
            return_value=True,
        ),
    ):
        result = await flow.async_step_user(
            {
                "url": submitted,
                "token": "test-only",
                "fallback_stt": "stt.standard",
                "fallback_tts": "tts.standard",
            }
        )
        assert result["step_id"] == "satellite"
        result = await flow.async_step_satellite(
            {
                "device": "device-a",
                "satellite": "satellite-a",
                "pipeline_select": "select.pipeline_a",
                "add_another": False,
            }
        )
    assert result["type"] == "create_entry"
    assert validated.call_args.args == (submitted, "test-only")
    stored = result["data"]["url"]
    assert stored == submitted
    assert (
        AssistLiveClient(stored, "test-only").url
        == AssistLiveClient(submitted, "test-only").url
    ), "setup sends the request that the flow validated"


@pytest.mark.parametrize(
    ("stored", "submitted"),
    [
        ("https://server.example", "https://server.example"),
        ("https://server.example", "https://server.example/"),
        ("https://server.example", "https://SERVER.Example"),
        ("https://server.example", "https://server.example:443"),
        ("https://Server.example:443/", "https://server.example"),
        ("https://server.example/api", "HTTPS://server.example:443/api/"),
    ],
    ids=["same", "slash", "host_case", "default_port", "legacy_raw", "path"],
)
async def test_same_server_and_token_aborts_without_change(
    hass, available_fallbacks, stored, submitted
):
    from homeassistant.data_entry_flow import AbortFlow
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    data = {
        "url": stored,
        "token": "test-only",
        "fallback_stt": "stt.standard",
        "fallback_tts": "tts.standard",
        "satellites": {"device-a": "satellite-a"},
    }
    existing = MockConfigEntry(domain="assist_live", data=data)
    existing.add_to_hass(hass)
    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.handler = "assist_live"
    with (
        patch("custom_components.assist_live.config_flow.AssistLiveClient") as client,
        pytest.raises(AbortFlow) as aborted,
    ):
        await flow.async_step_user(
            {
                "url": submitted,
                "token": "test-only",
                "fallback_stt": "stt.standard",
                "fallback_tts": "tts.standard",
            }
        )
    assert aborted.value.reason == "already_configured"
    client.assert_not_called()
    assert existing.data == data, "the existing entry does not change"
    assert len(hass.config_entries.async_entries("assist_live")) == 1


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_second_open_flow_aborts_after_first_creates_entry(
    configured_hass,  # noqa: F811
    client,
):
    import custom_components.assist_live as integration

    hass = configured_hass
    client.bindings.return_value = [
        {"satellite": "satellite-a"},
        {"satellite": "satellite-b"},
    ]
    user = {
        "url": "https://SERVER.example/",
        "token": "test-only",
        "fallback_stt": "stt.contract_stt",
        "fallback_tts": "tts.contract_tts",
    }
    with (
        patch(
            "custom_components.assist_live.config_flow.AssistLiveClient",
            return_value=client,
        ),
        patch(
            "custom_components.assist_live.config_flow.valid_satellite",
            return_value=True,
        ),
        # Entry setup is not part of this flow test.
        patch.object(integration, "async_setup_entry", AsyncMock(return_value=True)),
        patch.object(integration, "async_unload_entry", AsyncMock(return_value=True)),
    ):
        flows = []
        for _ in range(2):
            form = await hass.config_entries.flow.async_init(
                "assist_live", context={"source": "user"}
            )
            result = await hass.config_entries.flow.async_configure(
                form["flow_id"], user
            )
            assert result["step_id"] == "satellite", "both flows pass the user step"
            flows.append(result["flow_id"])
        first = await hass.config_entries.flow.async_configure(
            flows[0],
            {
                "device": "device-a",
                "satellite": "satellite-a",
                "pipeline_select": "select.pipeline_a",
                "add_another": False,
            },
        )
        assert first["type"] == "create_entry"
        second = await hass.config_entries.flow.async_configure(
            flows[1],
            {
                "device": "device-b",
                "satellite": "satellite-b",
                "pipeline_select": "select.pipeline_b",
                "add_another": False,
            },
        )
        assert second["type"] == "abort"
        assert second["reason"] == "already_configured"
        # The client removes only the trailing "/" from the URL text.
        assert first["data"]["url"] == "https://SERVER.example"
        entries = hass.config_entries.async_entries("assist_live")
        assert len(entries) == 1
        assert entries[0].data["satellites"] == {"device-a": "satellite-a"}
        assert await hass.config_entries.async_unload(entries[0].entry_id)


async def test_same_server_with_different_token_continues(hass, available_fallbacks):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    MockConfigEntry(
        domain="assist_live",
        data={
            "url": "https://server.example",
            "token": "test-only",
            "satellites": {"device-a": "satellite-a"},
        },
    ).add_to_hass(hass)
    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.handler = "assist_live"
    client = AsyncMock()
    client.bindings.return_value = [{"satellite": "satellite-b"}]
    with (
        patch(
            "custom_components.assist_live.config_flow.is_forwarder", return_value=False
        ),
        patch(
            "custom_components.assist_live.config_flow.AssistLiveClient",
            return_value=client,
        ),
    ):
        result = await flow.async_step_user(
            {
                "url": "https://server.example",
                "token": "second-test-only",
                "fallback_stt": "stt.standard",
                "fallback_tts": "tts.standard",
            }
        )
    assert result["step_id"] == "satellite"


@pytest.mark.parametrize(
    "submitted",
    [
        "https://server.example/v1/bindings?value",
        "https://server.example/v1/bindings#part",
    ],
)
async def test_refused_url_is_invalid_not_already_configured(
    hass, available_fallbacks, submitted
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    # Without the client rule, the appended bindings path goes into the query
    # or fragment of the submitted URL. Its key is then equal to the key of
    # this entry, and the flow aborts as already_configured.
    MockConfigEntry(
        domain="assist_live",
        data={
            "url": "https://server.example",
            "token": "test-only",
            "fallback_stt": "stt.standard",
            "fallback_tts": "tts.standard",
            "satellites": {"device-a": "satellite-a"},
        },
    ).add_to_hass(hass)
    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.handler = "assist_live"
    result = await flow.async_step_user(
        {
            "url": submitted,
            "token": "test-only",
            "fallback_stt": "stt.standard",
            "fallback_tts": "tts.standard",
        }
    )
    assert result["errors"] == {"base": "invalid_url"}
    assert len(hass.config_entries.async_entries("assist_live")) == 1


@pytest.mark.parametrize("url", ["not-a-server", "https://[broken"])
async def test_malformed_url_has_its_own_error(hass, available_fallbacks, url):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    result = await flow.async_step_user(
        {
            "url": url,
            "token": "test-only",
            "fallback_stt": "stt.standard",
            "fallback_tts": "tts.standard",
        }
    )
    assert result["errors"] == {"base": "invalid_url"}


@pytest.mark.parametrize("failure", ["empty", "protocol", "missing_host"])
async def test_user_reports_unusable_bindings(hass, available_fallbacks, failure):
    from assist_live_client.protocol import ProtocolError
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    MockConfigEntry(domain="assist_live", data={"token": "test-only"}).add_to_hass(hass)
    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.handler = "assist_live"
    url = "https:///no-host" if failure == "missing_host" else "https://server.example"
    user = {
        "url": url,
        "token": "test-only",
        "fallback_stt": "stt.standard",
        "fallback_tts": "tts.standard",
    }
    if failure == "missing_host":
        result = await flow.async_step_user(user)
    else:
        with patch(
            "assist_live_client.AssistLiveClient.bindings",
            return_value=[],
            side_effect=ProtocolError("invalid bindings")
            if failure == "protocol"
            else None,
        ):
            result = await flow.async_step_user(user)
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}
    assert not flow.bindings


@pytest.mark.parametrize(
    ("iot_class", "load_fails", "error"),
    [
        ("local_push", False, None),
        ("cloud_push", False, "cloud_destination"),
        (123, False, "unknown_destination"),
        (None, True, "unknown_destination"),
    ],
)
async def test_user_checks_fallback_manifest_before_accepting_local_only(
    hass, available_fallbacks, iot_class, load_fails, error
):
    from types import SimpleNamespace

    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er

    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    registry = er.async_get(hass)
    stt = registry.async_get_or_create("stt", "test", "standard").entity_id
    tts = registry.async_get_or_create("tts", "test", "standard").entity_id
    flow = AssistLiveConfigFlow()
    flow.hass = hass
    with (
        patch(
            "custom_components.assist_live.config_flow.async_get_integration",
            return_value=SimpleNamespace(manifest={"iot_class": iot_class}),
            side_effect=HomeAssistantError("integration missing")
            if load_fails
            else None,
        ),
        patch(
            "assist_live_client.AssistLiveClient.bindings",
            return_value=[{"satellite": "satellite-a"}],
        ),
    ):
        result = await flow.async_step_user(
            {
                "url": "https://server.example",
                "token": "test-only",
                "fallback_stt": stt,
                "fallback_tts": tts,
                "local_only": True,
            }
        )
    if error is None:
        assert result["step_id"] == "satellite"
    else:
        assert result["step_id"] == "user"
        assert result["errors"] == {"fallback_stt": error, "fallback_tts": error}


@pytest.mark.parametrize("url", ["http://server.example", "HTTP://server.example"])
async def test_http_warns_and_can_continue_without_refusal(
    hass, url, available_fallbacks
):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    with patch(
        "assist_live_client.AssistLiveClient.bindings",
        return_value=[{"satellite": "satellite-a"}],
    ) as bindings:
        result = await flow.async_step_user(
            {
                "url": url,
                "token": "test-only",
                "fallback_stt": "stt.standard",
                "fallback_tts": "tts.standard",
            }
        )
        assert result["step_id"] == "http_warning"
        bindings.assert_not_awaited()
        result = await flow.async_step_http_warning({})
        assert result["step_id"] == "satellite"
        bindings.assert_awaited_once()


async def test_binding_cannot_be_reused_by_another_entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    existing = MockConfigEntry(
        domain="assist_live", data={"satellites": {"device-a": "satellite-a"}}
    )
    existing.add_to_hass(hass)
    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.bindings = {"satellite-a"}
    with patch(
        "custom_components.assist_live.config_flow.valid_satellite", return_value=True
    ):
        result = await flow.async_step_satellite(
            {
                "device": "device-b",
                "satellite": "satellite-a",
                "pipeline_select": "select.pipeline_b",
                "add_another": False,
            }
        )
    assert result["errors"] == {"base": "binding_used"}


@pytest.mark.parametrize(
    ("domain", "device_matches", "has_satellite", "translation_key", "valid"),
    [
        ("select", True, True, "vad_sensitivity", False),
        ("select", True, True, "wake_word", False),
        ("select", True, True, "pipeline", True),
        ("select", True, True, "pipeline_n", True),
        ("sensor", True, True, "pipeline", False),
        ("select", False, True, "pipeline", False),
        ("select", True, False, "pipeline", False),
        (None, True, True, None, False),
    ],
)
async def test_satellite_form_requires_a_pipeline_control(
    hass, domain, device_matches, has_satellite, translation_key, valid
):
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    source = MockConfigEntry(domain="esphome")
    source.add_to_hass(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("esphome", "synthetic")}
    )
    other = devices.async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("esphome", "other")}
    )
    registry = er.async_get(hass)
    if has_satellite:
        registry.async_get_or_create(
            "assist_satellite", "esphome", "satellite", device_id=device.id
        )
    entity_id = "select.missing"
    if domain is not None:
        entity_id = registry.async_get_or_create(
            domain,
            "esphome",
            "selector",
            device_id=device.id if device_matches else other.id,
            translation_key=translation_key,
        ).entity_id
    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.handler = "assist_live"
    flow.settings = {"url": "https://server.example", "token": "test-only"}
    flow.bindings = {"satellite-a"}
    result = await flow.async_step_satellite(
        {"device": device.id, "satellite": "satellite-a", "pipeline_select": entity_id}
    )
    if valid:
        assert result["type"] == "create_entry"
        assert result["data"]["pipeline_selects"] == {device.id: entity_id}
    else:
        assert result.get("errors") == {"base": "invalid_satellite"}
        assert not flow.satellites


async def test_fifth_binding_finishes_even_if_add_another_is_checked(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    flow.handler = "assist_live"
    flow.settings = {"url": "https://server.example", "token": "test-only"}
    flow.bindings = {f"satellite-{index}" for index in range(6)}
    with patch(
        "custom_components.assist_live.config_flow.valid_satellite", return_value=True
    ):
        for index in range(5):
            result = await flow.async_step_satellite(
                {
                    "device": f"device-{index}",
                    "satellite": f"satellite-{index}",
                    "pipeline_select": f"select.pipeline_{index}",
                    "add_another": True,
                }
            )
            if index < 4:
                assert result["step_id"] == "satellite" and not result["errors"]
    assert result["type"] == "create_entry"
    assert result["data"]["satellites"] == {
        f"device-{index}": f"satellite-{index}" for index in range(5)
    }


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_upgrade_keeps_ids_and_pipeline_and_names_tts_in_other_language(
    configured_hass,  # noqa: F811
    client,
):
    import asyncio

    from assist_live_client.models import (
        HealthCapabilities,
        TTSAudio,
        TTSFormat,
        TTSSentenceEnd,
        TTSVoice,
    )
    from homeassistant.components import assist_pipeline, tts
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    hass = configured_hass

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    async def synth(turn_id, parts, voice, **kwargs):
        assert "".join([part async for part in parts]) == "Synthetic reply."
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(bytes(640))
        yield TTSSentenceEnd(0)

    client.events = events
    client.tts_stream = synth
    client.health_capabilities.return_value = HealthCapabilities(
        True,
        True,
        True,
        tts_voices=(TTSVoice("voice-two", "Server voice", "en-US"),),
        tts_default_voice="voice-two",
    )
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "url": "https://server.example",
            "token": "test-only",
            "fallback_stt": "stt.contract_stt",
            "fallback_tts": "tts.contract_tts",
            "satellites": {},
            "pipeline_selects": {},
        },
    )
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test", "satellite-a")},
        name="Synthetic satellite",
    )
    # Registry rows as the entities had them before has_entity_name.
    registry = er.async_get(hass)
    old = {
        unique_id: registry.async_get_or_create(
            domain,
            "assist_live",
            unique_id,
            suggested_object_id=object_id,
            config_entry=entry,
            device_id=device.id,
        ).entity_id
        for domain, unique_id, object_id in (
            ("stt", "satellite-a_stt", "assist_live_satellite_a"),
            ("tts", "satellite-a_tts", "assist_live_satellite_a"),
            (
                "sensor",
                "satellite-a_last_turn_latency",
                "assist_live_satellite_a_last_turn_latency",
            ),
        )
    }
    assert old == {
        "satellite-a_stt": "stt.assist_live_satellite_a",
        "satellite-a_tts": "tts.assist_live_satellite_a",
        "satellite-a_last_turn_latency": (
            "sensor.assist_live_satellite_a_last_turn_latency"
        ),
    }
    # The default pipeline settings must have the English of the probes.
    pipeline = await assist_pipeline.async_create_default_pipeline(
        hass, "stt.contract_stt", "tts.contract_tts", "assist-live satellite-a"
    )
    assert pipeline is not None
    await assist_pipeline.async_update_pipeline(
        hass,
        pipeline,
        stt_engine=old["satellite-a_stt"],
        tts_engine=old["satellite-a_tts"],
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "satellites": {device.id: "satellite-a"},
            "pipelines": {device.id: pipeline.id},
        },
    )
    before = {p.id for p in assist_pipeline.async_get_pipelines(hass)}
    # The integration has only translations/en.json, so HA must use English.
    hass.config.language = "de"
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            runtime = entry.runtime_data
            assert runtime.stt[device.id].entity_id == old["satellite-a_stt"]
            assert runtime.tts[device.id].entity_id == old["satellite-a_tts"]
            assert {
                row.unique_id: row.entity_id
                for row in er.async_entries_for_config_entry(registry, entry.entry_id)
            } == old, "registry entity IDs do not change"
            pipelines = {p.id: p for p in assist_pipeline.async_get_pipelines(hass)}
            assert set(pipelines) == before, "no new pipeline"
            assert pipelines[pipeline.id].stt_engine == old["satellite-a_stt"]
            assert pipelines[pipeline.id].tts_engine == old["satellite-a_tts"]
            assert entry.data["pipelines"] == {device.id: pipeline.id}
            tts_id = old["satellite-a_tts"]
            assert tts.async_resolve_engine(hass, tts_id) == tts_id
            assert (
                hass.states.get(tts_id).attributes["friendly_name"]
                == "Synthetic satellite TTS"
            )
            stream = tts.async_create_stream(
                hass,
                tts_id,
                language="en",
                options={
                    "preferred_format": "wav",
                    "preferred_sample_rate": 16000,
                    "preferred_sample_channels": 1,
                    "preferred_sample_bytes": 2,
                },
            )

            async def message():
                yield "Synthetic reply."

            stream.async_set_message_stream(message())
            delivered = b"".join([c async for c in stream.async_stream_result()])
            assert delivered.startswith(b"RIFF"), "HA found and named the engine"
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_runtime_subscribes_each_key_before_first_turn(hass, client):
    import asyncio

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime

    entered = {key: asyncio.Event() for key in ("satellite-a", "satellite-b")}
    queues = {key: asyncio.Queue() for key in entered}

    async def events(key, after_event_id=0):
        entered[key].set()
        while True:
            yield await queues[key].get()
            queues[key].task_done()

    client.events = events
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "satellites": {"device-a": "satellite-a", "device-b": "satellite-b"},
            "pipeline_selects": {},
        },
    )
    runtime = Runtime(hass, entry, client)
    await runtime.start()
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in entered.values())), 1
        )
        a = runtime.ledger.open_voice("satellite-a")
        b = runtime.ledger.open_voice("satellite-b")
        await queues["satellite-a"].put(
            {
                "event_id": 1,
                "event": {
                    "satellite": "satellite-a",
                    "turn_id": a.turn_id,
                    "stage": "playback_end",
                },
            }
        )
        await queues["satellite-a"].join()
        assert runtime.ledger.current("satellite-a") is None
        assert runtime.ledger.current("satellite-b") == b
    finally:
        await runtime.stop()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_real_entry_registers_pair_and_owns_pipeline(
    configured_hass,  # noqa: F811
    client,
    io_timeout,
):
    import asyncio

    from homeassistant.components import assist_pipeline
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    hass = configured_hass

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events
    client.bindings.return_value = [{"satellite": "satellite-a"}]
    from assist_live_client.models import HealthCapabilities, TTSVoice

    client.health_capabilities.return_value = HealthCapabilities(
        True,
        True,
        True,
        tts_voices=(TTSVoice("voice-two", "Server voice", "en-US"),),
        tts_default_voice="voice-two",
    )
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "url": "http://server.example",
            "token": "test-only",
            "fallback_stt": "stt.contract_stt",
            "fallback_tts": "tts.contract_tts",
            "satellites": {"device-a": "satellite-a"},
            "pipeline_selects": {},
            "voices": {"device-a": "voice-two"},
        },
    )
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test", "satellite-a")},
        name="Synthetic satellite",
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "satellites": {device.id: "satellite-a"},
            "voices": {device.id: "voice-two"},
        },
    )
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
    runtime = entry.runtime_data
    assert "assist_live" not in hass.data, "runtime state is in runtime_data only"
    assert runtime.tts[device.id].default_options == {"voice": "voice-two"}
    assert (
        runtime.tts[device.id].async_get_supported_voices("en")[0].name
        == "Server voice"
    )
    assert runtime.tts[device.id].fallback_voice is None
    stt_id = runtime.stt[device.id].entity_id
    tts_id = runtime.tts[device.id].entity_id
    assert hass.states.get(stt_id) is not None
    assert hass.states.get(tts_id) is not None
    assert (
        hass.states.get(stt_id).attributes["friendly_name"] == "Synthetic satellite STT"
    ), "STT name is in the context of its device"
    assert (
        hass.states.get(tts_id).attributes["friendly_name"] == "Synthetic satellite TTS"
    ), "TTS name is in the context of its device"
    assert er.async_get(hass).async_get(stt_id).device_id == device.id
    assert er.async_get(hass).async_get(tts_id).device_id == device.id
    saved = entry.data["pipelines"][device.id]
    pipeline = next(
        p for p in assist_pipeline.async_get_pipelines(hass) if p.id == saved
    )
    assert pipeline.stt_engine == stt_id
    assert pipeline.tts_engine == tts_id
    assert pipeline.tts_voice == "voice-two"
    from assist_live_client.models import TTSAudio, TTSFormat
    from homeassistant.components.tts import TTSAudioRequest

    selected_voices = []

    async def synth(turn_id, parts, voice, **kwargs):
        selected_voices.append(voice)
        assert [part async for part in parts] == ["synthetic reply"]
        yield TTSFormat("pcm_s16le", 22050, 1, 2)
        yield TTSAudio(bytes(2))

    async def reply():
        yield "synthetic reply"

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    client.tts_stream = synth
    response = await runtime.tts[device.id].async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": pipeline.tts_voice}, reply())
    )
    assert b"".join([chunk async for chunk in response.data_gen])
    assert selected_voices == ["voice-two"]
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", "assist_live", "satellite-a_last_turn_latency"
    )
    assert entity_id is not None
    sensor = registry.async_get(entity_id)
    assert sensor.disabled_by is None
    assert sensor.original_name == "Last turn latency"
    assert sensor.device_id == device.id
    state = hass.states.get(entity_id)
    assert state is not None
    assert {"turn_id", "turn_type"} <= state.attributes.keys()
    sending, cancelled = asyncio.Event(), asyncio.Event()

    async def hanging_event(event):
        sending.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client.event.side_effect = hanging_event
    turn = runtime.ledger.open_voice("satellite-a")
    assert await runtime.ledger.connect(turn)
    runtime.ledger.mark(turn.turn_id, "stt_final")
    runtime.ledger.schedule_events()
    await asyncio.wait_for(sending.wait(), 1)
    sender = runtime.ledger._event_sender
    assert await asyncio.wait_for(
        hass.config_entries.async_unload(entry.entry_id), io_timeout
    )
    assert cancelled.is_set()
    assert sender.done()
    assert not runtime.ledger._pending
    calls = client.event.await_count
    runtime.ledger.schedule_events(retry=True)
    assert client.event.await_count == calls
    client.event.side_effect = None
    before = {p.id for p in assist_pipeline.async_get_pipelines(hass)}
    with patch("custom_components.assist_live.AssistLiveClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert {p.id for p in assist_pipeline.async_get_pipelines(hass)} == before
    assert entry.data["pipelines"][device.id] == saved
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
def runtime_entry(monkeypatch):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    monkeypatch.setattr(
        "homeassistant.components.assist_pipeline.async_get_pipelines", lambda hass: []
    )

    return MockConfigEntry(
        domain="assist_live",
        data={
            "satellites": {"device-a": "satellite-a", "device-b": "satellite-b"},
            "pipeline_selects": {},
        },
    )


async def test_idle_maintenance_writes_only_changes_and_checks_health_on_schedule(
    hass, client, runtime_entry
):
    from custom_components.assist_live import Runtime

    runtime = Runtime(hass, runtime_entry, client)
    now = [100.0]
    runtime.clock = lambda: now[0]
    from assist_live_client.models import HealthCapabilities

    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    with (
        patch.object(
            runtime.store, "async_save", wraps=runtime.store.async_save
        ) as save,
        patch(
            "homeassistant.components.assist_pipeline.async_get_pipelines",
            return_value=[],
        ),
    ):
        for instant in (100, 101, 105, 110, 129.999):
            now[0] = instant
            await runtime._maintenance(None)
        assert save.await_count == 0
        assert client.health_capabilities.await_count == 1
        turn = runtime.ledger.open_voice("satellite-a")
        await runtime.ledger.connect(turn)
        assert save.await_count == 1
        now[0] = 130
        await runtime._maintenance(None)
        assert save.await_count == 1
        assert client.health_capabilities.await_count == 2
        client.health_capabilities.return_value = HealthCapabilities(False)
        for instant in (160, 164.999, 165):
            now[0] = instant
            await runtime._maintenance(None)
        assert client.health_capabilities.await_count == 4


async def test_event_cursors_are_debounced_and_resume_after_restart(
    hass, client, runtime_entry
):
    import asyncio

    from custom_components.assist_live import Runtime

    entered = {key: asyncio.Event() for key in ("satellite-a", "satellite-b")}
    queues = {key: asyncio.Queue() for key in entered}
    subscriptions = []

    async def events(key, after_event_id=0):
        subscriptions.append((key, after_event_id))
        entered[key].set()
        while True:
            row = await queues[key].get()
            try:
                yield row
            finally:
                queues[key].task_done()

    client.events = events
    runtime = Runtime(hass, runtime_entry, client)
    now = [0.0]
    runtime.clock = lambda: now[0]
    await runtime.start()
    try:
        with patch.object(
            runtime.store, "async_save", wraps=runtime.store.async_save
        ) as save:
            for key, event_id in (("satellite-a", 11), ("satellite-b", 22)):
                await queues[key].put(
                    {
                        "event_id": event_id,
                        "event": {
                            "satellite": key,
                            "turn_id": None,
                            "stage": "wake_detected",
                        },
                    }
                )
                await asyncio.wait_for(queues[key].join(), 1)
            assert save.await_count == 0
            now[0] = 5
            with patch(
                "homeassistant.components.assist_pipeline.async_get_pipelines",
                return_value=[],
            ):
                await runtime._maintenance(None)
            assert save.await_count == 1
            assert save.call_args.args[0] == {
                "version": 1,
                "turns": [],
                "cursors": {"satellite-a": 11, "satellite-b": 22},
            }
    finally:
        await runtime.stop()
    for event in entered.values():
        event.clear()
    restarted = Runtime(hass, runtime_entry, client)
    await restarted.start()
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in entered.values())), 1
        )
        assert subscriptions[-2:] == [("satellite-a", 11), ("satellite-b", 22)]
    finally:
        await restarted.stop()


@pytest.mark.parametrize(
    "broken",
    [
        {"turns": [], "cursors": 5},
        {"cursors": 5},
        {"turns": []},
        {"turns": [{"turn_id": "bad"}], "cursors": {}},
        {"turns": [{"satellite": "satellite-a"}], "cursors": {}},
        {"turns": [{"satellite": "satellite-a", "turn_id": "bad"}], "cursors": {}},
        {"version": 999, "turns": [], "cursors": {}},
        pytest.param(
            {
                "turns": [
                    {
                        "satellite": "satellite-z",
                        "turn_id": "01900000-0000-7000-8000-000000000001",
                    }
                ],
                "cursors": {},
            },
            id="foreign_satellite",
        ),
    ],
)
async def test_corrupt_store_does_not_block_setup(
    hass, client, runtime_entry, broken, caplog
):
    import asyncio

    from homeassistant.helpers import issue_registry as ir

    from custom_components.assist_live import Runtime

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events
    runtime = Runtime(hass, runtime_entry, client)
    with patch.object(runtime.store, "async_load", return_value=broken):
        await runtime.start()
    try:
        assert runtime.cursors == {}
        assert client.event.await_count == 0
        assert "Dropped invalid stored turn data" in caplog.text
        assert ir.async_get(hass).async_get_issue(
            "assist_live", f"{runtime_entry.entry_id}_invalid_stored_state"
        )
        assert "bad" not in caplog.text
    finally:
        await runtime.stop()


async def test_corrupt_store_keeps_valid_entries(hass, client, runtime_entry):
    import asyncio
    from uuid import uuid7

    from custom_components.assist_live import Runtime

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events
    turn_id = str(uuid7())
    runtime = Runtime(hass, runtime_entry, client)
    saved = {
        "turns": [
            {"satellite": "satellite-a", "turn_id": turn_id},
            {"satellite": "satellite-b"},
        ],
        "cursors": {"satellite-a": 7, "satellite-b": -1, "unknown": 9},
    }
    with patch.object(runtime.store, "async_load", return_value=saved):
        await runtime.start()
    try:
        assert runtime.cursors == {"satellite-a": 7}
        assert client.event.call_args.args[0].turn_id == turn_id
        await runtime._save()
        assert await runtime.store.async_load() == {
            "version": 1,
            "turns": [],
            "cursors": {"satellite-a": 7},
        }
    finally:
        await runtime.stop()


async def test_cursor_over_wire_limit_restarts_only_its_subscription(
    hass, client, runtime_entry
):
    import asyncio

    from homeassistant.helpers import issue_registry as ir

    from custom_components.assist_live import Runtime

    cursors = {}
    subscribed = asyncio.Event()

    async def events(satellite, *, after_event_id):
        cursors[satellite] = after_event_id
        if len(cursors) == 2:
            subscribed.set()
        await asyncio.Event().wait()
        yield

    client.events = events
    runtime = Runtime(hass, runtime_entry, client)
    maximum = (1 << 64) - 1
    saved = {"cursors": {"satellite-a": maximum + 1, "satellite-b": maximum}}
    with patch.object(runtime.store, "async_load", return_value=saved):
        await runtime.start()
    try:
        await subscribed.wait()
        assert cursors == {"satellite-a": 0, "satellite-b": maximum}
        assert ir.async_get(hass).async_get_issue(
            "assist_live", f"{runtime_entry.entry_id}_invalid_stored_state"
        )
    finally:
        await runtime.stop()


async def test_old_turn_list_store_keeps_restart_markers(hass, client, runtime_entry):
    import asyncio
    from uuid import uuid7

    from custom_components.assist_live import Runtime

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events

    turn_id = str(uuid7())
    runtime = Runtime(hass, runtime_entry, client)
    with patch.object(
        runtime.store,
        "async_load",
        return_value=[{"turn_id": turn_id, "satellite": "satellite-a"}],
    ):
        await runtime.start()
    try:
        assert client.event.call_args.args[0].turn_id == turn_id
        assert client.event.call_args.args[0].data == {"reason": "restart"}
        assert runtime.cursors == {}
    finally:
        await runtime.stop()


async def test_start_failure_cancels_tasks_and_removes_runtime(hass, client):
    import asyncio

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.assist_live import Runtime, async_setup_entry

    async def events(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    client.events = events
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "url": "https://server.example",
            "token": "test-only",
            "fallback_stt": "stt.standard",
            "fallback_tts": "tts.standard",
            "satellites": {"device-a": "satellite-a"},
            "pipeline_selects": {},
        },
    )
    captured = []

    def create(*args):
        runtime = Runtime(*args)
        captured.append(runtime)
        return runtime

    try:
        with (
            patch(
                "custom_components.assist_live.AssistLiveClient", return_value=client
            ),
            patch("custom_components.assist_live.Runtime", side_effect=create),
            patch(
                "custom_components.assist_live.async_track_time_interval",
                side_effect=RuntimeError("test start failure"),
            ),
        ):
            with pytest.raises(RuntimeError, match="test start failure"):
                await async_setup_entry(hass, entry)
        assert not hasattr(entry, "runtime_data")
        assert all(task.done() for task in captured[0].tasks)
        client.close.assert_awaited_once()
    finally:
        if hasattr(entry, "runtime_data"):
            del entry.runtime_data
            await captured[0].stop()


async def test_poison_event_retries_with_repair_backoff_and_no_cursor_skip(
    hass, client, runtime_entry, caplog, io_timeout
):
    import asyncio
    import logging

    from homeassistant.helpers import issue_registry as ir

    from custom_components.assist_live import Runtime

    runtime = Runtime(hass, runtime_entry, client)
    reached, release, processed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    delays, subscriptions = [], []
    poison = [True]

    async def events(key, after_event_id=0):
        if key == "satellite-b":
            await asyncio.Event().wait()
            return
        subscriptions.append(after_event_id)
        yield {
            "event_id": 7,
            "event": {
                "satellite": "satellite-b" if poison[0] else key,
                "turn_id": None,
                "stage": "wake_detected",
            },
        }
        processed.set()
        await asyncio.Event().wait()

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 6:
            reached.set()
            await release.wait()

    client.events = events
    runtime._sleep = sleep
    caplog.set_level(logging.DEBUG, logger="custom_components.assist_live")
    await runtime.start()
    try:
        await asyncio.wait_for(reached.wait(), 1)
        issue_id = f"{runtime_entry.entry_id}_satellite-a_events"
        assert ir.async_get(hass).async_get_issue("assist_live", issue_id)
        assert runtime.failures == {"satellite-a": 6}
        assert runtime.cursors.get("satellite-a", 0) == 0
        assert delays == [1, 2, 4, 8, 16, 30]
        retries = [
            record
            for record in caplog.records
            if record.name == "custom_components.assist_live"
            and record.levelno == logging.DEBUG
            and "retry" in record.message.lower()
        ]
        assert len(retries) == 6
        poison[0] = False
        release.set()
        await asyncio.wait_for(processed.wait(), io_timeout)
        assert subscriptions == [0] * 7
        assert runtime.cursors["satellite-a"] == 7
        assert runtime.failures["satellite-a"] == 0
        assert ir.async_get(hass).async_get_issue("assist_live", issue_id) is None
    finally:
        await runtime.stop()


@pytest.mark.parametrize(
    ("data", "valid"),
    [
        ({}, True),
        ({"pre_roll_ms": 0}, True),
        ({"pre_roll_ms": 300}, True),
        ({"pre_roll_ms": 1000}, True),
        ({"pre_roll_ms": -1}, False),
        ({"pre_roll_ms": 1001}, False),
        ({"pre_roll_ms": True}, False),
        ([], False),
    ],
)
async def test_client_timing_history_event_reaches_ledger_or_retries(
    hass, client, runtime_entry, data, valid
):
    import asyncio
    import json
    from unittest.mock import AsyncMock, MagicMock

    import aiohttp
    from assist_live_client import AssistLiveClient

    from custom_components.assist_live import Runtime

    runtime = Runtime(hass, runtime_entry, client)
    turn = runtime.ledger.open_voice("satellite-a")
    reached = asyncio.Event()
    delays = []
    envelope = {
        "version": 1,
        "event_id": 7,
        "event": {
            "turn_id": turn.turn_id,
            "satellite": "satellite-a",
            "source": "device",
            "stage": "mic_open",
            "t_utc_ns": 123,
            "quality": "measured",
            "uncertainty_ms": None,
            "data": data,
        },
    }

    async def messages():
        yield aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(envelope), "")
        reached.set()
        await asyncio.Event().wait()

    async def sleep(delay):
        delays.append(delay)
        reached.set()
        await asyncio.Event().wait()

    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.__aiter__.side_effect = messages
    connection = AsyncMock()
    connection.__aenter__.return_value = ws
    real_client = AssistLiveClient("http://server.invalid", "test-token")
    runtime.client = real_client
    runtime._save = AsyncMock()
    runtime._sleep = sleep
    with patch.object(real_client, "_connect", return_value=connection):
        task = asyncio.create_task(runtime._events("satellite-a"))
        try:
            await asyncio.wait_for(reached.wait(), 1)
            assert not task.done()
            if valid:
                assert runtime.cursors == {"satellite-a": 7}
                assert runtime.failures == {"satellite-a": 0}
                assert runtime.ledger.records[turn.turn_id].timings["mic_open"] == {
                    "t_utc_ns": 123,
                    "quality": "measured",
                    "uncertainty_ms": None,
                    "source": "device",
                }
                assert delays == []
            else:
                assert runtime.cursors == {}
                assert runtime.failures == {"satellite-a": 1}
                assert "mic_open" not in runtime.ledger.records[turn.turn_id].timings
                assert delays == [1.0]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await real_client.close()


@pytest.mark.parametrize("silent", [False, True])
async def test_store_failure_does_not_acknowledge_event(
    hass, client, runtime_entry, silent, io_timeout
):
    import asyncio

    from custom_components.assist_live import Runtime

    runtime = Runtime(hass, runtime_entry, client)
    reached, release, processed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    runtime.clock = lambda: 10.0
    subscriptions = []

    async def events(key, after_event_id=0):
        if key == "satellite-b":
            await asyncio.Event().wait()
            return
        subscriptions.append(after_event_id)
        yield {
            "event_id": 9,
            "event": {"satellite": key, "turn_id": None, "stage": "wake_detected"},
        }
        processed.set()
        await asyncio.Event().wait()

    async def sleep(delay):
        reached.set()
        await release.wait()

    original_save = runtime.store.async_save
    failed = [False]

    async def save(data):
        if not failed[0]:
            failed[0] = True
            if silent:
                return
            raise OSError("test store failure")
        await original_save(data)

    client.events = events
    runtime._sleep = sleep
    with patch.object(runtime.store, "async_save", side_effect=save):
        await runtime.start()
        try:
            await asyncio.wait_for(reached.wait(), 1)
            assert runtime.cursors.get("satellite-a", 0) == 0
            assert not runtime.tasks[0].done()
            release.set()
            await asyncio.wait_for(processed.wait(), io_timeout)
            assert subscriptions == [0, 0]
            assert runtime.cursors["satellite-a"] == 9
        finally:
            await runtime.stop()


async def test_maintenance_store_failure_uses_backoff(hass, client, runtime_entry):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.assist_live import Runtime

    runtime = Runtime(hass, runtime_entry, client)
    now = [100.0]
    runtime.clock = lambda: now[0]
    runtime.ledger.open_voice("satellite-a")
    with patch.object(
        runtime.store, "async_save", side_effect=OSError("test store failure")
    ) as save:
        for instant in (100, 100.5, 101, 102, 103):
            now[0] = instant
            await runtime._maintenance(None)
        assert save.await_count == 3
        assert runtime.failures == {"satellite-a": 3, "satellite-b": 3}
        for key in ("satellite-a", "satellite-b"):
            assert ir.async_get(hass).async_get_issue(
                "assist_live", f"{runtime_entry.entry_id}_{key}_events"
            )


def test_real_store_file_failure_is_detected_and_raises_repair(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from textwrap import dedent

    # A subprocess excludes the HA pytest fixture that replaces Store I/O.
    program = dedent("""
        import asyncio
        import sys
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import issue_registry as ir
        from homeassistant.helpers.storage import Store
        from homeassistant.util.file import WriteError
        from assist_live_client.models import HealthCapabilities
        from custom_components.assist_live import Runtime

        class Client:
            async def health_capabilities(self, **kwargs):
                return HealthCapabilities(True, True, True)
            async def close(self):
                pass

        async def main():
            hass = HomeAssistant(sys.argv[1])
            await ir.async_get(hass).async_load()
            entry = SimpleNamespace(entry_id="disk-test", options={}, data={
                "satellites": {"device-a": "satellite-a"}, "pipeline_selects": {},
            })
            runtime = Runtime(hass, entry, Client())
            assert type(runtime.store) is Store
            now = [100.0]
            runtime.clock = lambda: now[0]
            initial = {"version": 1, "turns": [], "cursors": {}}
            await runtime.store.async_save(initial)
            assert await runtime.store.async_load() == initial
            path = Path(runtime.store.path)
            before = path.read_bytes()
            runtime.ledger.open_voice("satellite-a")
            try:
                with (
                    patch("homeassistant.helpers.storage.write_utf8_file",
                          side_effect=WriteError("test file-layer failure")) as write,
                    patch("homeassistant.components.assist_pipeline.async_get_pipelines",
                          return_value=[]),
                ):
                    for instant in (100, 101, 103):
                        now[0] = instant
                        await runtime._maintenance(None)
                    assert write.call_count == 3
                    assert runtime.failures["satellite-a"] == 3
                    assert runtime._last_saved == initial
                    assert ir.async_get(hass).async_get_issue(
                        "assist_live", "disk-test_satellite-a_events")
                    assert path.read_bytes() == before
                print("PASS real Store: 3 file-layer failures detected; "
                      "unchanged disk; repair raised")
            finally:
                await runtime.stop()
                await hass.async_stop(force=True)
        asyncio.run(main())
    """)
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "PASS real Store: 3 file-layer failures detected; unchanged disk; repair raised"
        in result.stdout
    )
