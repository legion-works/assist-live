from unittest.mock import AsyncMock

import pytest

import custom_components.assist_live  # noqa: F401


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
