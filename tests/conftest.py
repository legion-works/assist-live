from unittest.mock import AsyncMock

import pytest

import custom_components.assist_live  # noqa: F401


@pytest.fixture
def client():
    from assist_live_client import AssistLiveClient

    result = AsyncMock(spec=AssistLiveClient)
    result.health.return_value = False
    return result


@pytest.fixture
def ledger(client):
    from custom_components.assist_live.ledger import TurnLedger

    return TurnLedger(client)
