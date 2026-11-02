import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from contextlib import suppress
from typing import Literal, TypedDict

from assist_live_client import AssistLiveClient
from assist_live_client.models import HealthCapabilities
from homeassistant.components.homeassistant import exposed_entities
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


def build_name_hints(
    areas: Iterable[str], entities: Iterable[str]
) -> tuple[list[str], int]:
    groups = []
    for values in (areas, entities):
        group = {value.lower().strip() for value in values}
        groups.append(
            {
                name
                for name in group
                if 1 <= len(name) <= 64 and any(char.isalpha() for char in name)
            }
        )
    names = sorted(groups[0]) + sorted(groups[1] - groups[0])
    return names[:500], max(0, len(names) - 500)


def hints_digest(names: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(names, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class HintStatus(TypedDict):
    count: int
    digest: str
    not_sent: int
    last_result: Literal["sent", "skipped_no_support", "failed"]
    last_send: str | None


class STTHints:
    def __init__(
        self, hass: HomeAssistant, client: AssistLiveClient, satellites: Iterable[str]
    ) -> None:
        self.hass = hass
        self.client = client
        self.status: dict[str, HintStatus] = {
            satellite: {
                "count": 0,
                "digest": hints_digest([]),
                "not_sent": 0,
                "last_result": "skipped_no_support",
                "last_send": None,
            }
            for satellite in satellites
        }
        self.failures = 0
        self._names: list[str] = []
        self._ready = False
        self._active = False
        self._unsub: list[Callable[[], None]] = []
        self._exposure_unsub: Callable[[], None] | None = None
        self._debounce: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._health: HealthCapabilities | None = None
        self._pending: set[str] = set()
        self._rebuild = False

    @callback
    def start(self) -> None:
        self._active = True
        if "homeassistant" in self.hass.config.components:
            try:
                self._collect()
            except Exception as error:
                self._failed(self.status, error)
        self._unsub.extend(
            [
                self.hass.bus.async_listen(
                    er.EVENT_ENTITY_REGISTRY_UPDATED, self._changed
                ),
                self.hass.bus.async_listen(
                    ar.EVENT_AREA_REGISTRY_UPDATED, self._changed
                ),
            ]
        )

    @callback
    def _changed(self, _: object = None) -> None:
        if not self._active:
            return
        if self._debounce is not None:
            self._debounce()
        self._debounce = async_call_later(self.hass, 5, self._after_change)

    @callback
    def _after_change(self, _: object) -> None:
        self._debounce = None
        self._rebuild = True
        self._request(set(self.status))

    @callback
    def observe_health(self, health: HealthCapabilities) -> None:
        self._health = health
        if not self._active:
            return
        if not health.stt_hints:
            for row in self.status.values():
                row["last_result"] = "skipped_no_support"
            return
        reported = health.stt_hints_by_satellite or {}
        self._request(
            {
                satellite
                for satellite, row in self.status.items()
                if row["last_result"] != "sent"
                or reported.get(satellite, {}).get("digest") != row["digest"]
            }
        )

    @callback
    def _request(self, satellites: set[str]) -> None:
        self._pending.update(satellites)
        if self._pending and (self._task is None or self._task.done()):
            self._task = self.hass.async_create_task(
                self._send(), "assist-live-stt-hints"
            )

    def _collect(self) -> None:
        if self._exposure_unsub is None:
            self._exposure_unsub = exposed_entities.async_listen_entity_updates(
                self.hass, "conversation", self._changed
            )
        registry = er.async_get(self.hass)
        names = []
        for state in self.hass.states.async_all():
            if exposed_entities.async_should_expose(
                self.hass, "conversation", state.entity_id
            ):
                names.extend(
                    intent.async_get_entity_aliases(
                        self.hass, registry.async_get(state.entity_id), state=state
                    )
                )
        self._names, not_sent = build_name_hints(
            (area.name for area in ar.async_get(self.hass).async_list_areas()), names
        )
        digest = hints_digest(self._names)
        for row in self.status.values():
            row["count"] = len(self._names)
            row["digest"] = digest
            row["not_sent"] = not_sent
        self._ready = True

    def _failed(self, satellites: Iterable[str], error: Exception) -> None:
        for satellite in satellites:
            self.status[satellite]["last_result"] = "failed"
            self.failures += 1
        _LOGGER.debug("STT hints failed: %s", type(error).__name__)

    async def _send(self) -> None:
        while self._pending:
            satellites, self._pending = self._pending, set()
            try:
                if self._rebuild:
                    self._rebuild = False
                    self._health = await self.client.health_capabilities(timeout=0.2)
                    self._ready = False
                if self._health is None or not self._health.stt_hints:
                    for satellite in satellites:
                        self.status[satellite]["last_result"] = "skipped_no_support"
                    continue
                if not self._ready:
                    self._collect()
            except Exception as error:
                self._failed(satellites, error)
                continue
            names = self._names
            for satellite in sorted(satellites):
                if self._health is None or not self._health.stt_hints:
                    self.status[satellite]["last_result"] = "skipped_no_support"
                    continue
                try:
                    async with asyncio.timeout(2):
                        await self.client.set_hints(satellite, names)
                except Exception as error:
                    self._failed([satellite], error)
                else:
                    self.status[satellite]["last_result"] = "sent"
                    self.status[satellite]["last_send"] = dt_util.utcnow().isoformat()

    async def stop(self) -> None:
        self._active = False
        if self._debounce is not None:
            self._debounce()
            self._debounce = None
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._exposure_unsub is not None:
            self._exposure_unsub()
            self._exposure_unsub = None
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._pending.clear()
        self._names = []
