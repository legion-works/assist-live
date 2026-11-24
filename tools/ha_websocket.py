from collections.abc import Mapping
from urllib.parse import urlsplit
from weakref import WeakSet

import aiohttp

HA_PORT = 18123
HA_URL = f"http://127.0.0.1:{HA_PORT}"
_guarded_sessions: WeakSet[aiohttp.ClientSession] = WeakSet()


def checked_ha_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != HA_PORT
        or parsed.path not in ("", "/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("live checks require the isolated loopback test instance")
    return f"http://127.0.0.1:{HA_PORT}"


async def _refuse_redirects(
    request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
) -> aiohttp.ClientResponse:
    response = await handler(request)
    if 300 <= response.status < 400:
        response.close()
        raise ValueError("HTTP or WebSocket redirect refused")
    return response


def ha_session(*, headers: Mapping[str, str] | None = None) -> aiohttp.ClientSession:
    session = aiohttp.ClientSession(headers=headers, middlewares=(_refuse_redirects,))
    _guarded_sessions.add(session)
    return session


def require_ha_session(session: aiohttp.ClientSession) -> None:
    if session not in _guarded_sessions:
        raise ValueError("HA connection requires a session with redirect refusal")
