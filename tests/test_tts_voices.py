from unittest.mock import patch

import pytest
from assist_live_client.models import HealthCapabilities, TTSAudio, TTSFormat, TTSVoice
from assist_live_client.protocol import ProtocolError
from homeassistant.components.tts import TTSAudioRequest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_tts_forward import Fallback, entity, text


def health(*voices, default=None):
    return HealthCapabilities(
        healthy=True,
        stt=True,
        tts=True,
        text_positions=False,
        local_only=None,
        destinations=None,
        tts_voices=tuple(
            TTSVoice(id=voice, name=name, language=language)
            for voice, name, language in voices
        ),
        tts_default_voice=default,
    )


@pytest.mark.parametrize(
    "language,expected",
    [
        ("en", ["server-us", "server-gb", "server-en"]),
        ("en-US", ["server-us", "server-en"]),
        ("EN_us", ["server-us", "server-en"]),
        ("en-GB", ["server-gb", "server-en"]),
        ("fr", ["server-fr"]),
        ("de", []),
    ],
)
async def test_voice_list_uses_cached_server_languages(
    client, ledger, language, expected
):
    ledger.last_health = health(
        ("server-us", "US", "en-US"),
        ("server-gb", "GB", "en-GB"),
        ("server-en", "English", "en"),
        ("server-fr", "French", "fr-FR"),
        default="server-us",
    )
    forward = entity(client, ledger, Fallback())
    assert [
        voice.voice_id for voice in forward.async_get_supported_voices(language)
    ] == expected
    assert forward.async_get_supported_voices("en-US")[0].name == "US"
    assert forward.default_options == {"voice": "server-us"}
    assert set(forward.supported_languages) == {"en-US", "en-GB", "en", "fr-FR", "fr"}
    assert forward.default_language == "en-US"
    client.health_capabilities.assert_not_awaited()
    ledger.last_health = health(("next", "Next", "de-DE"), default="next")
    assert forward.default_options == {"voice": "next"}
    assert [v.voice_id for v in forward.async_get_supported_voices("de")] == ["next"]
    assert "en" not in forward.supported_languages


async def test_older_health_has_no_voices_or_default(client, ledger):
    ledger.last_health = HealthCapabilities(True, True, True)
    fallback = Fallback()
    fallback._attr_default_options = {"voice": "voice-1"}
    forward = entity(client, ledger, fallback)
    assert forward.async_get_supported_voices("en") == []
    assert not forward.default_options


async def test_default_options_use_server_default(client, ledger):
    ledger.last_health = health(("server-us", "US", "en-US"), default="server-us")
    assert entity(client, ledger, Fallback()).default_options == {"voice": "server-us"}


@pytest.mark.parametrize("with_list", [True, False])
@pytest.mark.parametrize("requested_voice", [None, "server-us", "server-retired"])
async def test_no_voice_reaches_fake_server_without_voice_field(
    hass, socket_enabled, with_list, requested_voice
):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from assist_live_client import AssistLiveClient
    from assist_live_client.protocol import encode_audio, encode_control

    from custom_components.assist_live.ledger import TurnLedger

    received = []

    async def capabilities(request):
        return web.json_response(
            {
                "version": 1,
                "healthy": True,
                "capabilities": {
                    "tts": True,
                    **(
                        {
                            "tts_voices": [
                                {"id": "server-us", "name": "US", "language": "en-US"}
                            ],
                            "tts_default_voice": "server-us",
                        }
                        if with_list
                        else {}
                    ),
                },
            }
        )

    async def accepted(request):
        return web.json_response({})

    async def stream(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hello = await ws.receive_json()
        received.append(hello)
        turn = hello["turn_id"]
        await ws.send_json(encode_control("negotiated", turn, capabilities=["tts"]))
        if hello.get("voice") == "server-retired":
            await ws.send_json(encode_control("error", turn, code="unknown_tts_voice"))
            await ws.close()
            return ws
        await ws.send_json(
            encode_control(
                "format", turn, codec="pcm_s16le", rate=22050, channels=1, width=2
            )
        )
        await ws.send_bytes(encode_audio(turn, 0, b"\x01\x00"))
        await ws.send_json(encode_control("sentence_end", turn, sentence=0))
        await ws.send_json(encode_control("end", turn))
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/health", capabilities)
    app.router.add_post("/v1/turns", accepted)
    app.router.add_post("/v1/event", accepted)
    app.router.add_get("/v1/audio", stream)
    async with (
        TestServer(app) as server,
        AssistLiveClient(str(server.make_url("")), "test-only") as client,
    ):
        ledger = TurnLedger(client)
        fallback = Fallback()
        forward = entity(client, ledger, fallback)
        forward.hass = hass
        forward.entity_id = "tts.assist_live_test"
        response = await forward.async_stream_tts_audio(
            TTSAudioRequest(
                "en",
                {} if requested_voice is None else {"voice": requested_voice},
                text(),
            )
        )
        output = b"".join([part async for part in response.data_gen])
        if requested_voice == "server-retired":
            assert output == b"fallback"
            assert fallback.voice == "voice-1"
            assert [i.translation_key for i in ir.async_get(hass).issues.values()] == [
                "unknown_tts_voice"
            ]
        else:
            assert output[44:] == b"\x01\x00"
            assert not hasattr(fallback, "voice")
        if requested_voice is None:
            assert "voice" not in received[0]
        else:
            assert received[0]["voice"] == requested_voice
        assert bool(forward.async_get_supported_voices("en")) is with_list


@pytest.mark.parametrize("selected", [None, "voice-2"])
async def test_fallback_uses_only_its_selected_or_default_voice(
    client, ledger, selected
):
    fallback = Fallback()
    fallback._attr_default_options = {"voice": "voice-1"}
    forward = entity(client, ledger, fallback)
    forward.fallback_voice = selected
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "server-only", "other": "kept"}, text())
    )
    assert b"".join([chunk async for chunk in response.data_gen]) == b"fallback"
    assert fallback.voice == (selected or "voice-1")
    assert ledger.latest["satellite-a"].voice == (selected or "voice-1")


@pytest.mark.parametrize("recovery", ["request", "health"])
async def test_unknown_voice_falls_back_and_repair_clears(
    hass, client, ledger, recovery
):
    initial = health(
        ("server-current", "Current", "en-US"),
        ("server-retired", "Stale", "en-US"),
        default="server-current",
    )
    client.health_capabilities.return_value = initial
    forward = entity(client, ledger, Fallback())
    forward.hass = hass
    forward.entity_id = "tts.assist_live_test"
    refused = True

    async def synth(turn, parts, voice, *, text_positions=False):
        assert voice == "server-retired"
        if refused:
            raise ProtocolError("unknown_tts_voice")
        yield TTSFormat("pcm_s16le", 16000, 1, 2)
        yield TTSAudio(b"\x01\x00")

    client.tts_stream = synth
    response = await forward.async_stream_tts_audio(
        TTSAudioRequest("en", {"voice": "server-retired"}, text())
    )
    assert b"".join([part async for part in response.data_gen]) == b"fallback"
    issues = ir.async_get(hass)
    matching = [
        issue
        for issue in issues.issues.values()
        if issue.translation_key == "unknown_tts_voice"
    ]
    assert len(matching) == 1
    issue = matching[0]
    assert issue.translation_placeholders == {
        "voice": "server-retired",
        "entity": "tts.assist_live_test",
    }
    assert "First sentence" not in str(issue)
    forward.async_get_supported_voices("en")
    assert issue in issues.issues.values()
    if recovery == "health":
        ledger.last_health = health(("server-retired", "Restored", "en-US"))
        forward.async_get_supported_voices("en")
    else:
        refused = False
        response = await forward.async_stream_tts_audio(
            TTSAudioRequest("en", {"voice": "server-retired"}, text())
        )
        assert b"".join([part async for part in response.data_gen])[44:] == b"\x01\x00"
    assert not [
        i for i in issues.issues.values() if i.translation_key == "unknown_tts_voice"
    ]


async def test_voice_repair_sweep_runs_once_per_voice_list(hass, client):
    from custom_components.assist_live import Runtime

    entry = MockConfigEntry(
        domain="assist_live", data={"satellites": {}, "pipeline_selects": {}}
    )
    runtime = Runtime(hass, entry, client)
    forward = entity(client, runtime.ledger, Fallback())
    forward.hass = hass
    forward.entity_id = "tts.assist_live_test"
    runtime.tts["device-a"] = forward
    now = [100.0]
    runtime.clock = lambda: now[0]
    client.health_capabilities.return_value = health(("server-us", "US", "en-US"))
    issue_id = f"{forward.unique_id}_unknown_tts_voice_server-retired"
    ir.async_create_issue(
        hass,
        "assist_live",
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="unknown_tts_voice",
        translation_placeholders={
            "voice": "server-retired",
            "entity": forward.entity_id,
        },
    )
    with (
        patch(
            "homeassistant.components.assist_pipeline.async_get_pipelines",
            return_value=[],
        ),
        patch(
            "custom_components.assist_live.tts.ir.async_delete_issue",
            wraps=ir.async_delete_issue,
        ) as delete,
    ):

        def voice_deletes():
            return [
                call
                for call in delete.call_args_list
                if "_unknown_tts_voice_" in call.args[2]
            ]

        await runtime._maintenance(None)
        first_sweep = len(voice_deletes())
        assert first_sweep == 1
        previous = client.health_capabilities.return_value
        client.health_capabilities.return_value = health(("server-us", "US", "en-US"))
        assert client.health_capabilities.return_value == previous
        assert client.health_capabilities.return_value is not previous
        now[0] = 130.0
        await runtime._maintenance(None)
        assert client.health_capabilities.await_count == 2
        forward.async_get_supported_voices("en")
        assert len(voice_deletes()) == first_sweep
        assert ir.async_get(hass).async_get_issue("assist_live", issue_id) is not None
        client.health_capabilities.return_value = health(
            ("server-retired", "Restored", "en-US")
        )
        now[0] = 160.0
        await runtime._maintenance(None)
        assert len(voice_deletes()) == first_sweep + 1
        assert ir.async_get(hass).async_get_issue("assist_live", issue_id) is None


@pytest.mark.parametrize(
    "voices,default",
    [
        ((("server-next", "US", "en-US"),), None),
        ((("server-us", "Next", "en-US"),), None),
        ((("server-us", "US", "en-GB"),), None),
        ((("server-us", "US", "en-US"),), "server-us"),
    ],
    ids=["id", "name", "language", "default"],
)
async def test_voice_metadata_changes_trigger_repair_sweep(
    hass, client, ledger, voices, default
):
    forward = entity(client, ledger, Fallback())
    forward.hass = hass
    ledger.last_health = health(("server-us", "US", "en-US"))
    with patch(
        "custom_components.assist_live.tts.ir.async_delete_issue",
        wraps=ir.async_delete_issue,
    ) as delete:
        forward.async_update_voices()
        delete.assert_called_once()
        ledger.last_health = health(*voices, default=default)
        forward.async_update_voices()
        assert delete.call_count == 2
        forward.async_update_voices()
        assert delete.call_count == 2


async def test_options_select_voice_from_fallback_engine(hass):
    from custom_components.assist_live.config_flow import AssistLiveConfigFlow

    entry = MockConfigEntry(
        domain="assist_live",
        data={"fallback_tts": "tts.standard", "fallback_stt": "stt.standard"},
    )
    entry.add_to_hass(hass)
    flow = AssistLiveConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    with patch(
        "custom_components.assist_live.tts.get_engine_instance", return_value=Fallback()
    ):
        form = await flow.async_step_init()
        schema = form["data_schema"]
        assert schema({})["fallback_tts_voice"] == ""
        field = next(k for k in schema.schema if str(k) == "fallback_tts_voice")
        options = schema.schema[field].config["options"]
        assert options == [
            {"value": "", "label": "Default"},
            {"value": "voice-1", "label": "One"},
            {"value": "voice-2", "label": "Two"},
        ]
        invalid = await flow.async_step_init({"fallback_tts_voice": "server-only"})
        assert invalid["errors"]["fallback_tts_voice"] == "invalid_fallback_voice"
        result = await flow.async_step_init({"fallback_tts_voice": "voice-2"})
    assert result["type"] == "create_entry"
    assert result["data"]["fallback_tts_voice"] == "voice-2"


async def test_diagnostics_report_server_voice_metadata(hass, client):
    from custom_components.assist_live import Runtime
    from custom_components.assist_live.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(domain="assist_live", data={"satellites": {}})
    runtime = Runtime(hass, entry, client)
    runtime.ledger.last_health = health(
        ("server-us", "US", "en-US"), default="server-us"
    )
    hass.data["assist_live"] = {entry.entry_id: runtime}
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["server_tts_voice_count"] == 1
    assert result["server_tts_default_voice"] == "server-us"
    runtime.ledger.last_health = HealthCapabilities(True, True, True)
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["server_tts_voice_count"] == 0
    assert result["server_tts_default_voice"] is None


async def test_health_maintenance_refreshes_entity_and_fallback_option(hass, client):
    from custom_components.assist_live import Runtime

    entry = MockConfigEntry(
        domain="assist_live", data={"satellites": {}, "pipeline_selects": {}}
    )
    runtime = Runtime(hass, entry, client)
    forward = entity(client, runtime.ledger, Fallback())
    runtime.tts["device-a"] = forward
    capabilities = health(("server-us", "US", "en-US"), default="server-us")
    client.health_capabilities.return_value = capabilities
    with patch(
        "homeassistant.components.assist_pipeline.async_get_pipelines", return_value=[]
    ):
        await runtime._maintenance(None)
    assert runtime.ledger.last_health is capabilities
    assert forward.default_options == {"voice": "server-us"}
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={"fallback_tts_voice": "voice-2"}
    )
    runtime.update_options(entry)
    assert forward.fallback_voice == "voice-2"
    hass.config_entries.async_update_entry(entry, options={})
    runtime.update_options(entry)
    assert forward.fallback_voice is None


async def test_public_health_refresh_updates_cache_on_schedule(hass, client):
    from custom_components.assist_live import Runtime

    entry = MockConfigEntry(
        domain="assist_live", data={"satellites": {}, "pipeline_selects": {}}
    )
    runtime = Runtime(hass, entry, client)
    refresh = getattr(runtime, "refresh_health", None)
    assert callable(refresh)
    now = [100.0]
    runtime.clock = lambda: now[0]
    initial = health(("server-us", "US", "en-US"))
    client.health_capabilities.return_value = initial
    await refresh()
    assert runtime.ledger.last_health is initial
    await refresh()
    client.health_capabilities.assert_awaited_once_with(timeout=0.2)
    changed = health(("server-next", "Next", "en-US"))
    client.health_capabilities.return_value = changed
    now[0] = 130.0
    with (
        patch(
            "homeassistant.components.assist_pipeline.async_get_pipelines",
            return_value=[],
        ),
        patch.object(runtime, "refresh_health", wraps=refresh) as public_refresh,
    ):
        await runtime._maintenance(None)
    public_refresh.assert_awaited_once_with()
    assert client.health_capabilities.await_count == 2
    assert runtime.ledger.last_health is changed
