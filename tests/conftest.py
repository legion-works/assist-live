import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import custom_components.assist_live  # noqa: F401


@pytest.fixture
def available_fallbacks():
    from test_stt_forward import Fallback as SttFallback
    from test_tts_forward import Fallback as TtsFallback

    with (
        patch(
            "homeassistant.components.stt.async_get_speech_to_text_entity",
            return_value=SttFallback(),
        ),
        patch(
            "custom_components.assist_live.tts.get_engine_instance",
            return_value=TtsFallback(),
        ),
    ):
        yield


@pytest.fixture
def client():
    from assist_live_client import AssistLiveClient
    from assist_live_client.models import HealthCapabilities

    result = AsyncMock(spec=AssistLiveClient)
    result.health.return_value = False
    result.health_capabilities.return_value = HealthCapabilities(False, False, False)
    return result


@pytest.fixture
def ledger(client):
    from custom_components.assist_live.ledger import TurnLedger

    return TurnLedger(client)


@pytest.fixture
def drain_timing_events():
    async def drain(ledger):
        async with asyncio.timeout(1):
            if ledger._event_sender is not None:
                await asyncio.shield(ledger._event_sender)
            assert not ledger._pending

    return drain
