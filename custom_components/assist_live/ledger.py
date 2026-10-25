import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid7

from assist_live_client import AssistLiveClient
from assist_live_client.models import TimingEvent, TurnOpen
from assist_live_client.protocol import ProtocolError


@dataclass
class Record:
    turn: TurnOpen
    opened: float
    status: str = "open"
    closed: float | None = None
    stages: set[str] = field(default_factory=set)


class TurnLedger:
    def __init__(
        self, client: AssistLiveClient, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.client = client
        self.clock = clock
        self.records: dict[str, Record] = {}
        self._active: dict[str, str] = {}
        self._previous: dict[str, Record] = {}
        self._pending: list[TimingEvent] = []
        self._connected: set[str] = set()
        self.save: Callable[[], Awaitable[None]] | None = None

    def current(self, satellite: str) -> TurnOpen | None:
        if turn_id := self._active.get(satellite):
            record = self.records[turn_id]
            if self.clock() - record.opened < 120:
                return record.turn
            self._close(record, "incomplete")
        return None

    def _close(self, record: Record, status: str) -> None:
        record.status = status
        record.closed = self.clock()
        self._active.pop(record.turn.satellite, None)
        self._previous[record.turn.satellite] = record
        self.prune()

    def prune(self) -> None:
        # The 10 s parent window also covers the server's 5 s reorder window.
        closed = [
            record for record in self.records.values() if record.closed is not None
        ]
        remove = {
            record.turn.turn_id
            for record in closed
            if record.closed is not None and self.clock() - record.closed >= 10
        }
        remaining = [record for record in closed if record.turn.turn_id not in remove]
        parents = {record.turn.turn_id for record in self._previous.values()}
        evictable = [
            record for record in remaining if record.turn.turn_id not in parents
        ]
        remove.update(
            record.turn.turn_id for record in evictable[: max(0, len(remaining) - 128)]
        )
        for turn_id in remove:
            self.records.pop(turn_id)
            self._connected.discard(turn_id)
        self._previous = {
            satellite: record
            for satellite, record in self._previous.items()
            if record.turn.turn_id in self.records
        }
        self._pending = [
            event for event in self._pending if event.turn_id in self.records
        ]

    def _open(self, satellite: str, kind: Literal["voice", "tts_only"]) -> TurnOpen:
        self.prune()
        if current := self.current(satellite):
            self.mark(current.turn_id, "turn_superseded")
            self._close(self.records[current.turn_id], "incomplete")
        previous = self._previous.get(satellite)
        parent = (
            previous.turn.turn_id
            if previous is not None
            and previous.closed is not None
            and self.clock() - previous.closed < 10
            else None
        )
        turn = TurnOpen(str(uuid7()), satellite, kind, parent, None)
        self.records[turn.turn_id] = Record(turn, self.clock())
        self._active[satellite] = turn.turn_id
        return turn

    def open_voice(self, satellite: str) -> TurnOpen:
        return self._open(satellite, "voice")

    def open_tts(self, satellite: str) -> TurnOpen:
        current = self.current(satellite)
        if current and current.turn_type == "voice":
            if "stt_final" in self.records[current.turn_id].stages:
                return current
        return self._open(satellite, "tts_only")

    def mark(self, turn_id: str, stage: str) -> None:
        record = self.records.get(turn_id)
        if record is None:
            return
        record.stages.add(stage)
        self._pending.append(
            TimingEvent(
                turn_id,
                record.turn.satellite,
                "integration",
                stage,
                time.time_ns(),
                "measured",
                None,
            )
        )

    def satellite_state(self, satellite: str, state: str) -> None:
        if turn := self.current(satellite):
            self.records[turn.turn_id].stages.add("satellite_state")
            self._pending.append(
                TimingEvent(
                    turn.turn_id,
                    satellite,
                    "ha_state",
                    "satellite_state",
                    time.time_ns(),
                    "proxy",
                    None,
                    data={"state": state},
                )
            )

    def device_event(self, satellite: str, turn_id: str | None, stage: str) -> None:
        turn = self.current(satellite)
        if turn is None or turn.turn_id != turn_id:
            return
        self.records[turn.turn_id].stages.add(stage)
        if stage == "playback_end":
            self._close(self.records[turn.turn_id], "complete")

    async def connect(self, turn: TurnOpen) -> bool:
        if self.save is not None:
            await self.save()
        if turn.turn_id in self._connected:
            return True
        try:
            async with asyncio.timeout(10):
                await self.client.open_turn(turn)
            if turn.turn_id not in self.records:
                return False
            self._connected.add(turn.turn_id)
            await self.flush()
            return True
        except ProtocolError, TimeoutError:
            return False

    async def flush(self) -> None:
        pending, self._pending = self._pending, []
        for event in pending:
            if event.turn_id not in self._connected:
                continue
            try:
                await self.client.event(event)
            except ProtocolError:
                pass

    def snapshot(self) -> list[dict[str, str]]:
        return [
            {"turn_id": turn_id, "satellite": satellite}
            for satellite, turn_id in self._active.items()
        ]

    async def restore(self, rows: list[dict[str, str]]) -> None:
        for row in rows:
            try:
                await self.client.event(
                    TimingEvent(
                        row["turn_id"],
                        row["satellite"],
                        "integration",
                        "turn_superseded",
                        time.time_ns(),
                        "measured",
                        None,
                        data={"reason": "restart"},
                    )
                )
            except ProtocolError:
                pass
