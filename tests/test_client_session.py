import pytest
from aiohttp import web
from assist_live_client import AssistLiveClient
from assist_live_client.models import TTSAudio, TTSFormat, TTSSentenceEnd
from assist_live_client.protocol import ProtocolError, encode_audio, encode_control
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.assist_live.ledger import stream_failure_reason

TURN = "01936b2a-7c1d-7abc-8def-0123456789ab"


async def text():
    yield "Hello."


@pytest.fixture
async def audio_server(socket_enabled, io_timeout):
    paths = []
    headers = []

    async def audio(request):
        paths.append(request.path)
        headers.append(request.headers)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hello = await ws.receive_json(timeout=io_timeout)
        assert hello["type"] == "negotiate"
        turn = hello["turn_id"]
        await ws.send_json(encode_control("negotiated", turn, capabilities=["tts"]))
        while (await ws.receive_json(timeout=io_timeout))["type"] != "end":
            pass
        await ws.send_json(
            encode_control(
                "format", turn, codec="pcm_s16le", rate=16000, channels=1, width=2
            )
        )
        await ws.send_bytes(encode_audio(turn, 0, b"\x01\x00"))
        await ws.send_json(encode_control("sentence_end", turn, sentence=0, text_end=6))
        await ws.send_json(encode_control("end", turn))
        await ws.close()
        return ws

    async def redirect(request):
        paths.append(request.path)
        raise web.HTTPTemporaryRedirect("/target/v1/audio")

    app = web.Application()
    app.router.add_get("/v1/audio", audio)
    app.router.add_get("/target/v1/audio", audio)
    app.router.add_get("/redirect/v1/audio", redirect)
    runner = web.AppRunner(app, handler_cancellation=False)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        _, port = runner.addresses[0]
        yield f"http://127.0.0.1:{port}", paths, headers
    finally:
        await runner.cleanup()


async def test_ha_shared_session_refuses_redirect_and_still_works(
    hass, audio_server, relaxed_message_deadline
):
    base, paths, headers = audio_server
    session = async_get_clientsession(hass)
    connector = session.connector
    assert connector is not None

    async with AssistLiveClient(
        f"{base}/redirect", "test-only", session=session
    ) as wire:
        with pytest.raises(ProtocolError, match="^redirect refused$") as caught:
            _ = [item async for item in wire.tts_stream(TURN, text())]
        assert caught.value.kind == "refused"
        assert stream_failure_reason(caught.value) == "stream_refused"
        assert paths == ["/redirect/v1/audio"]
        assert not session.closed and not connector.closed

    assert session.connector is connector
    assert not session.closed and not connector.closed

    async with AssistLiveClient(base, "test-only", session=session) as wire:
        items = [
            item async for item in wire.tts_stream(TURN, text(), text_positions=True)
        ]
    assert items == [
        TTSFormat("pcm_s16le", 16000, 1, 2),
        TTSAudio(b"\x01\x00"),
        TTSSentenceEnd(0, text_end=6),
    ]
    assert paths == ["/redirect/v1/audio", "/v1/audio"]
    assert headers[0]["Authorization"].split() == ["Bearer", "test-only"]
    assert headers[0]["User-Agent"].startswith("HomeAssistant")
    assert session.connector is connector
    assert not session.closed and not connector.closed


async def test_ha_shared_session_with_closed_connector(hass, audio_server):
    base, paths, _ = audio_server
    session = async_get_clientsession(hass)
    connector = session.connector
    assert connector is not None
    await connector.close()

    async with AssistLiveClient(base, "test-only", session=session) as wire:
        with pytest.raises(ProtocolError, match="^session closed$") as caught:
            _ = [item async for item in wire.tts_stream(TURN, text())]
        assert caught.value.kind == "closed"
        assert stream_failure_reason(caught.value) == "stream_closed"
    assert paths == []
