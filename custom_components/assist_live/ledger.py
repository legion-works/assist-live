import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid7

from assist_live_client import AssistLiveClient
from assist_live_client.models import HealthCapabilities, TimingEvent, TurnOpen
from assist_live_client.protocol import ProtocolError


@dataclass
class Record:
    turn: TurnOpen
    opened: float
    status: str = "open"
    closed: float | None = None
    stages: set[str] = field(default_factory=set)
    timings: dict[str, dict[str, Any]] = field(default_factory=dict)
    destinations: list[dict[str, str]] = field(default_factory=list)
    speaker: dict[str, Any] | None = None
    voice: str | None = None
    audio_format: dict[str, Any] | None = None


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
        self.failures: dict[str, int] = {}
        self.late_server_failures = 0
        self.save: Callable[[], Awaitable[None]] | None = None
        self.record_transcripts = False
        self.latest: dict[str, Record] = {}
        self.listeners: set[Callable[[], None]] = set()
        self.speaker_event: Callable[[dict[str, Any]], None] | None = None
        self.fallback_policy: Callable[[str], Awaitable[str]] | None = None
        self.speaker_reads: dict[str, asyncio.Task[None]] = {}
        self.speaker_missing = 0

    def server_selected(
        self, health: HealthCapabilities, part: Literal["stt", "tts"]
    ) -> bool:
        return health.healthy is True and getattr(health, part) is True

    async def stop_speaker_reads(self) -> None:
        tasks = list(self.speaker_reads.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def current(self, satellite: str) -> TurnOpen | None:
        if turn_id := self._active.get(satellite):
            record = self.records[turn_id]
            if self.clock() - record.opened < 120:
                return record.turn
            self._close(record, "incomplete")
        return None

    def _close(self, record: Record, status: str) -> None:
        if task := self.speaker_reads.get(record.turn.turn_id):
            task.cancel()
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
        turn = TurnOpen(
            str(uuid7()), satellite, kind, parent, None, self.record_transcripts
        )
        self.records[turn.turn_id] = Record(turn, self.clock())
        self.latest[satellite] = self.records[turn.turn_id]
        self._active[satellite] = turn.turn_id
        self.changed()
        return turn

    def changed(self) -> None:
        for listener in tuple(self.listeners):
            listener()

    async def use_fallback(self, turn_id: str, part: str, entity_id: str) -> None:
        entity_id = entity_id or "unknown"
        mode = (
            await self.fallback_policy(entity_id) if self.fallback_policy else "unknown"
        )
        self.used(turn_id, part, entity_id, mode)

    def used(self, turn_id: str, part: str, provider: str, mode: str) -> None:
        if record := self.records.get(turn_id):
            row = {"part": part, "provider": provider, "destination": mode}
            if row not in record.destinations:
                record.destinations.append(row)
            self.changed()

    def speaker_result(self, turn_id: str, result: dict[str, Any]) -> None:
        record = self.records.get(turn_id)
        if record is None or record.speaker is not None:
            return
        record.speaker = {
            key: result[key] for key in ("name", "distance", "destination")
        }
        self.used(turn_id, "speaker", "speaker_plugin", result["destination"])
        if self.speaker_event is not None:
            self.speaker_event(
                {
                    "turn_id": turn_id,
                    "satellite": record.turn.satellite,
                    **record.speaker,
                }
            )

    def observe(self, event: dict[str, Any]) -> None:
        record = self.records.get(event.get("turn_id", ""))
        if record is None or record.turn.satellite != event["satellite"]:
            return
        stage = event["stage"]
        data = event.get("data", {})
        keep_local_end = (
            stage == "end_decision"
            and record.timings.get(stage, {}).get("decision_source") == "ha_audio_end"
            and data.get("decision_source") != "ha_audio_end"
        )
        if "t_utc_ns" in event and not keep_local_end:
            record.timings[stage] = {
                key: event.get(key)
                for key in ("t_utc_ns", "quality", "uncertainty_ms", "source")
            }
        if stage == "end_decision" and stage in record.timings and not keep_local_end:
            record.timings[stage]["decision_source"] = data.get(
                "decision_source",
                "server_detector" if event.get("source") == "server" else "unknown",
            )
        if data.get("part") in {
            "stt",
            "tts",
            "speaker",
            "stt_fallback",
            "tts_fallback",
        }:
            part = data["part"]
            mode = data.get("destination", "unknown")
            provider = data.get("provider", "unknown")
            if mode in {"local", "cloud", "unknown"} and isinstance(provider, str):
                placeholder = next(
                    (
                        row
                        for row in record.destinations
                        if row["part"] == part and row["provider"] == "server"
                    ),
                    None,
                )
                if placeholder is not None:
                    placeholder.update(provider=provider, destination=mode)
                else:
                    self.used(record.turn.turn_id, part, provider, mode)
        self.device_event(event["satellite"], event.get("turn_id"), stage)
        self.changed()

    def open_voice(self, satellite: str) -> TurnOpen:
        return self._open(satellite, "voice")

    def open_tts(self, satellite: str) -> TurnOpen:
        current = self.current(satellite)
        if current and current.turn_type == "voice":
            if "stt_final" in self.records[current.turn_id].stages:
                return current
        return self._open(satellite, "tts_only")

    def mark(
        self, turn_id: str, stage: str, *, decision_source: str | None = None
    ) -> None:
        record = self.records.get(turn_id)
        if record is None:
            return
        if stage in {"stt_failed", "tts_failed", "fallback_used"}:
            self.failures[stage] = self.failures.get(stage, 0) + 1
        record.stages.add(stage)
        data: dict[str, object] = {}
        if decision_source is not None:
            data["decision_source"] = decision_source
        if (
            stage == "fallback_used"
            and record.destinations
            and record.destinations[-1]["part"].endswith("_fallback")
        ):
            data.update(record.destinations[-1])
        self._pending.append(
            TimingEvent(
                turn_id,
                record.turn.satellite,
                "integration",
                stage,
                time.time_ns(),
                "proxy" if decision_source is not None else "measured",
                None,
                data=data,
            )
        )
        timing = {
            "t_utc_ns": self._pending[-1].t_utc_ns,
            "quality": self._pending[-1].quality,
            "uncertainty_ms": None,
            "source": "integration",
            "decision_source": decision_source,
        }
        if not (
            decision_source == "server_detector"
            and record.timings.get(stage, {}).get("source") == "server"
        ):
            record.timings[stage] = timing
        self.changed()

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
