import asyncio
from unittest.mock import patch

import probatio as vol
import pytest
import test_fallback_lifecycle
import test_options_fallback
import test_pipeline_contract
from aiohttp import web
from assist_live_client.models import HealthCapabilities
from assist_live_client.protocol import ProtocolError
from homeassistant.components import stt
from homeassistant.components.tts import TTSAudioRequest
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntryState,
)
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_stt_forward import Fallback as SttFallback
from test_stt_forward import audio, metadata
from test_tts_forward import Fallback as TtsFallback
from test_tts_forward import text

fallback_entry = test_options_fallback.fallback_entry
lifecycle_entry = test_fallback_lifecycle.lifecycle_entry
pipeline_hass = test_pipeline_contract.pipeline_hass


def make_entry(hass, url):
    entry = MockConfigEntry(
        domain="assist_live",
        data={
            "url": url,
            "token": "test-only",
            "fallback_stt": "stt.contract_stt",
            "fallback_tts": "tts.contract_tts",
            "fallback_tts_voice": "voice-one",
            "satellites": {"device-a": "satellite-a"},
        },
    )
    entry.add_to_hass(hass)
    return entry


async def assert_fallback_turns(entry):
    runtime = entry.runtime_data
    result = await runtime.stt["device-a"].async_process_audio_stream(
        metadata(), audio([bytes(320)])
    )
    assert result.text == "test command"
    response = await runtime.tts["device-a"].async_stream_tts_audio(
        TTSAudioRequest("en", {}, text())
    )
    assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"


@pytest.mark.parametrize("status", [401, 403])
async def test_setup_checks_bindings_without_blocking_fallback(
    pipeline_hass,
    enable_custom_integrations,
    socket_enabled,
    relaxed_health_timeout,
    status,
):
    hass = pipeline_hass
    requests = []

    async def bindings(request):
        requests.append(request.headers["Authorization"].split()[1])
        return web.Response(status=status)

    app = web.Application()
    app.router.add_get("/v1/bindings", bindings)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        _, port = runner.addresses[0]
        entry = make_entry(hass, f"http://127.0.0.1:{port}")
        original_stt = stt.async_get_speech_to_text_entity(hass, "stt.contract_stt")
        from custom_components.assist_live.tts import fallback_engine

        original_tts = fallback_engine(hass, "tts.contract_tts")
        with (
            patch.object(
                original_stt,
                "async_process_audio_stream",
                side_effect=SttFallback().async_process_audio_stream,
            ),
            patch.object(
                original_tts,
                "async_stream_tts_audio",
                side_effect=TtsFallback().async_stream_tts_audio,
            ),
            patch.object(type(entry), "async_start_reauth", autospec=True) as start,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            try:
                assert requests == ["test-only"], "SETUP_DID_NOT_READ_BINDINGS"
                assert start.call_count == int(status == 401), "SETUP_AUTH_NOT_HANDLED"
                await assert_fallback_turns(entry)
                assert start.call_count == int(status == 401)
            finally:
                assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        await runner.cleanup()


async def test_concurrent_unauthorized_requests_start_reauth_once(
    lifecycle_entry, client
):
    hass, entry = lifecycle_entry
    runtime = entry.runtime_data
    runtime._health_due = 0
    both_started = asyncio.Event()
    attempts = 0

    async def unauthorized(**_):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            both_started.set()
        await both_started.wait()
        raise ProtocolError("request refused", kind="unauthorized")

    client.health_capabilities.side_effect = unauthorized
    with patch.object(type(entry), "async_start_reauth", autospec=True) as start:
        await asyncio.gather(runtime.refresh_health(), runtime.refresh_health())
        assert start.call_args_list == [((entry, hass), {})], "REAUTH_NOT_STARTED_ONCE"
        client.health_capabilities.side_effect = None
        client.health_capabilities.return_value = HealthCapabilities(True, True, True)
        client.health_capabilities.reset_mock()
        await assert_fallback_turns(entry)
        client.health_capabilities.assert_not_awaited()
        assert not runtime.server_healthy


async def test_event_upgrade_unauthorized_starts_reauth(
    pipeline_hass,
    enable_custom_integrations,
    socket_enabled,
    aiohttp_server,
    io_timeout,
):
    hass = pipeline_hass
    entered = asyncio.Event()
    release = asyncio.Event()

    async def bindings(_request):
        return web.json_response([{"satellite": "satellite-a"}])

    async def events(_request):
        entered.set()
        await release.wait()
        return web.Response(status=401)

    app = web.Application()
    app.router.add_get("/v1/bindings", bindings)
    app.router.add_get("/v1/events", events)
    server = await aiohttp_server(app)
    entry = make_entry(hass, str(server.make_url("/")))

    async def unexpected_retry(_delay):
        pytest.fail("UNAUTHORIZED_EVENT_STREAM_RETRIED")

    with patch.object(type(entry), "async_start_reauth", autospec=True) as start:
        assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            runtime = entry.runtime_data
            await asyncio.wait_for(entered.wait(), io_timeout)
            runtime._sleep = unexpected_retry
            release.set()
            await runtime.tasks[0]
            start.assert_called_once_with(entry, hass)
        finally:
            release.set()
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reauth_starts_after_reconfigure_closes(lifecycle_entry, client):
    hass, entry = lifecycle_entry
    runtime = entry.runtime_data
    flows = hass.config_entries.flow
    form = await flows.async_init(
        "assist_live",
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    client.health_capabilities.side_effect = ProtocolError(
        "request refused", kind="unauthorized"
    )
    runtime._health_due = 0
    await runtime.refresh_health()
    assert not list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    flows.async_abort(form["flow_id"])
    await runtime._maintenance(None)
    await runtime._maintenance(None)
    await hass.async_block_till_done()
    active = list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert len(active) == 1, "REAUTH_NOT_RESUMED_AFTER_RECONFIGURE"
    flows.async_abort(active[0]["flow_id"])


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("unauthorized", "invalid_auth"), ("refused", "cannot_connect")],
)
async def test_config_flow_distinguishes_unauthorized(
    hass, available_fallbacks, client, kind, expected
):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    flow = AssistLiveConfigFlow()
    flow.hass = hass
    client.bindings.side_effect = ProtocolError("request refused", kind=kind)
    with patch(
        "custom_components.assist_live.config_flow.AssistLiveClient",
        return_value=client,
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
    assert result["errors"] == {"base": expected}, "CONFIG_AUTH_ERROR_MISMATCH"
    client.close.assert_awaited_once()


async def begin_reauth(hass, entry):
    form = await hass.config_entries.flow.async_init(
        "assist_live",
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert form["step_id"] == "reauth_confirm"
    return form["flow_id"]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("unauthorized", "invalid_auth"),
        ("refused", "cannot_connect"),
        ("missing", "wrong_server"),
        ("duplicate", "token_in_use"),
        ("duplicate_during_check", "token_in_use"),
        ("url_changed", "server_changed"),
        ("invalid_url", "invalid_url"),
    ],
)
async def test_reauth_refuses_without_updating_or_reloading(
    pipeline_hass, client, enable_custom_integrations, outcome, expected
):
    hass = pipeline_hass
    entry = make_entry(
        hass, "invalid" if outcome == "invalid_url" else "https://server.example/api"
    )
    old_data = dict(entry.data)

    def add_duplicate():
        MockConfigEntry(
            domain="assist_live",
            data={"url": "https://SERVER.example:443/api/", "token": "replacement"},
        ).add_to_hass(hass)

    if outcome == "duplicate":
        add_duplicate()

    async def bindings():
        if outcome == "duplicate_during_check":
            add_duplicate()
        if outcome == "url_changed":
            hass.config_entries.async_update_entry(
                entry, data={**old_data, "url": "https://new.example"}
            )
        if outcome in {"unauthorized", "refused"}:
            raise ProtocolError("request refused", kind=outcome)
        if outcome == "missing":
            return [{"satellite": "satellite-b"}]
        return [{"satellite": "satellite-a"}]

    client.bindings.side_effect = bindings
    flow_id = await begin_reauth(hass, entry)
    with (
        patch(
            "custom_components.assist_live.config_flow.AssistLiveClient",
            return_value=client,
            side_effect=ValueError("invalid URL") if outcome == "invalid_url" else None,
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"token": "replacement"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": expected}, "REAUTH_TOKEN_NOT_REFUSED"
    if outcome == "url_changed":
        assert entry.data == {**old_data, "url": "https://new.example"}
    else:
        assert entry.data == old_data
    reload.assert_not_called()
    if outcome in {"duplicate", "invalid_url"}:
        client.bindings.assert_not_awaited()
    else:
        client.bindings.assert_awaited_once()
        client.close.assert_awaited_once()
    hass.config_entries.flow.async_abort(flow_id)


@pytest.mark.parametrize("replace", [True, False])
async def test_reauth_preserves_entry_and_reloads(
    lifecycle_entry, client, replace, caplog
):
    hass, entry = lifecycle_entry
    runtime = entry.runtime_data
    old_data = dict(entry.data)
    client.health_capabilities.side_effect = ProtocolError(
        "request refused", kind="unauthorized"
    )
    runtime._health_due = 0
    await runtime.refresh_health()
    await hass.async_block_till_done()
    flows = list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert len(flows) == 1, "UNAUTHORIZED_DID_NOT_START_REAUTH"
    flow_id = flows[0]["flow_id"]
    form = await hass.config_entries.flow.async_configure(flow_id)
    fields = form["data_schema"].schema
    assert {str(field) for field in fields} == {"token"}
    assert all(field.default is vol.UNDEFINED for field in fields)
    token = "replacement" if replace else old_data["token"]
    MockConfigEntry(
        domain="assist_live", data={"url": "https://other.example", "token": token}
    ).add_to_hass(hass)
    client.bindings.return_value = [
        {"satellite": "satellite-a"},
        {"satellite": "satellite-b"},
    ]
    client.health_capabilities.side_effect = None
    client.health_capabilities.return_value = HealthCapabilities(True, True, True)
    with (
        patch(
            "custom_components.assist_live.config_flow.AssistLiveClient",
            return_value=client,
        ) as validate,
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload,
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"token": token}
        )
        await hass.async_block_till_done()
    assert validate.call_args.args == (old_data["url"], token)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert result.get("translation_domain") in (None, "assist_live")
    assert entry.data == {**old_data, "token": token}
    assert entry.runtime_data is not runtime, "REAUTH_DID_NOT_RELOAD"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.server_healthy
    reload.assert_awaited_once_with(entry.entry_id)
    assert "should use it for scheduling a reload" not in caplog.text
