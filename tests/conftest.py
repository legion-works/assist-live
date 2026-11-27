import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

import custom_components.assist_live  # noqa: F401


@pytest.fixture
def relaxed_health_timeout(monkeypatch, io_timeout):
    # HTTP setup does not test the 200 ms health deadline.
    for module in ("", ".stt", ".tts", ".stt_hints"):
        monkeypatch.setattr(
            f"custom_components.assist_live{module}.HEALTH_TIMEOUT", io_timeout
        )


@pytest.fixture
def io_timeout():
    return 10.0


@pytest.fixture
def relaxed_message_deadline(monkeypatch, io_timeout):
    # These protocol tests check content and order, not receive latency.
    monkeypatch.setattr("assist_live_client.client.MESSAGE_DEADLINE", io_timeout)
    monkeypatch.setattr(
        "custom_components.assist_live.stt.MESSAGE_DEADLINE", io_timeout
    )


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


@pytest.fixture
def recovery_clock(monkeypatch):
    from custom_components.assist_live import tts

    class RecoveryClock:
        now = 0
        deadline = 0
        active = None
        armed = asyncio.Event()

        @asynccontextmanager
        async def timeout(self, delay):
            async with asyncio.timeout(None) as timer:
                self.active = timer
                self.deadline = self.now + delay
                self.armed.set()
                try:
                    yield
                finally:
                    self.active = None

        def advance(self, step):
            self.now += step
            if self.active is None or self.now < self.deadline:
                return False
            self.active.reschedule(asyncio.get_running_loop().time())
            return True

    clock = RecoveryClock()

    class Asyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        def timeout(self, delay):
            if delay == tts.RECOVERY_TEXT_TIMEOUT:
                return clock.timeout(delay)
            return asyncio.timeout(delay)

    monkeypatch.setattr(tts, "RECOVERY_TEXT_TIMEOUT", 100)
    monkeypatch.setattr(tts, "asyncio", Asyncio())
    return clock
