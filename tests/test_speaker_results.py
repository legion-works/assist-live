import asyncio
from contextlib import asynccontextmanager, suppress
from uuid import uuid7

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from assist_live_client import AssistLiveClient
from assist_live_client import client as client_module
from assist_live_client.protocol import encode_control
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_stt_forward import Fallback, audio, metadata

from custom_components.assist_live import Runtime
from custom_components.assist_live.diagnostics import async_get_config_entry_diagnostics
from custom_components.assist_live.stt import AssistLiveSTT


class SpeakerWire:
    def __init__(self, hass, monkeypatch):
        self.hass = hass
        self.queues = {}
        self.decoded = asyncio.Queue()
        self.entities = []
        self.seen = []
        self.event_queue = asyncio.Queue()
        self.processed = asyncio.Queue()
        self.event_id = 0
        clock = hass.loop.time
        self.offset = 0.0
        monkeypatch.setattr(hass.loop, "time", lambda: clock() + self.offset)
        decode = client_module.decode_control

        def observed(*args, **kwargs):
            value = decode(*args, **kwargs)
            self.decoded.put_nowait(value)
            return value

        monkeypatch.setattr(client_module, "decode_control", observed)
        hass.bus.async_listen(
            "assist_live_speaker", callback(lambda event: self.seen.append(event.data))
        )

    async def health(self, request):
        return web.json_response(
            {
                "version": 1,
                "healthy": True,
                "capabilities": {"stt": True},
                "local_only": True,
                "destinations": {"stt": "local"},
            }
        )

    async def accepted(self, request):
        return web.json_response({})

    async def events(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive_json()

        async def receive():
            async for _ in ws:
                pass

        receiver = asyncio.create_task(receive())
        try:
            while (value := await self.event_queue.get()) is not None:
                await ws.send_json(value)
        finally:
            receiver.cancel()
            with suppress(asyncio.CancelledError):
                await receiver
            await ws.close()
        return ws

    async def stream(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        turn_id = (await ws.receive_json())["turn_id"]
        queue = self.queues[turn_id] = asyncio.Queue()

        async def receive():
            async for _ in ws:
                pass

        receiver = asyncio.create_task(receive())
        try:
            await ws.send_json(
                encode_control(
                    "negotiated", turn_id, capabilities=["stt", "speaker_result"]
                )
            )
            await ws.send_json(
                encode_control("final", turn_id, text="  synthetic command é\n")
            )
            while (value := await queue.get()) is not None:
                await ws.send_json(value)
        except ConnectionResetError:
            pass
        finally:
            receiver.cancel()
            with suppress(asyncio.CancelledError):
                await receiver
            await ws.close()
        return ws

    async def start(self, satellite="satellite-a"):
        entity = AssistLiveSTT(
            satellite,
            "device-" + satellite,
            self.client,
            self.runtime.ledger,
            Fallback(),
        )
        self.entities.append(entity)
        result = await entity.async_process_audio_stream(
            metadata(), audio([bytes(320)])
        )
        assert result.text.encode() == "  synthetic command é\n".encode()
        turn = self.runtime.ledger.latest[satellite].turn
        await self.send(turn.turn_id, "heartbeat")
        return turn.turn_id

    async def send(self, turn_id, kind, **fields):
        self.queues[turn_id].put_nowait(encode_control(kind, turn_id, **fields))
        while True:
            message = await asyncio.wait_for(self.decoded.get(), 1)
            if message["type"] == kind and message["turn_id"] == turn_id:
                break
        await self.hass.async_block_till_done()

    async def advance(self, seconds):
        steps = round(seconds * 10)
        for _ in range(steps):
            self.offset = round(self.offset + 0.1, 6)
            for turn_id, read in list(self.runtime.ledger.speaker_reads.items()):
                if not read.task.done():
                    self.queues[turn_id].put_nowait(
                        encode_control("heartbeat", turn_id)
                    )
            await self.hass.async_block_till_done()
            while not self.decoded.empty():
                self.decoded.get_nowait()

    async def result(self, turn_id, *, name="synthetic-speaker", destination="cloud"):
        await self.send(
            turn_id, "speaker_result", name=name, distance=0.25, destination=destination
        )
        await self.hass.async_block_till_done()

    def issue(self):
        return ir.async_get(self.hass).async_get_issue(
            "assist_live", f"{self.entry.entry_id}_speaker_destination"
        )

    def timing(self, turn_id, *, name="synthetic-speaker", destination="cloud"):
        return {
            "turn_id": turn_id,
            "satellite": "satellite-a",
            "stage": "speaker_result",
            "source": "server",
            "t_utc_ns": 1,
            "quality": "measured",
            "uncertainty_ms": None,
            "data": {
                **({"name": name} if name else {}),
                "distance": 0.25,
                "destination": destination,
            },
        }

    async def timing_result(self, event):
        if not self.runtime.tasks:

            async def saved(*, cursor, **kwargs):
                self.processed.put_nowait(cursor)

            self.runtime._save = saved
            self.runtime.tasks.append(
                asyncio.create_task(self.runtime._events("satellite-a"))
            )
        self.event_id += 1
        self.event_queue.put_nowait(
            {"version": 1, "event_id": self.event_id, "event": event}
        )
        assert await asyncio.wait_for(self.processed.get(), 1) == (
            "satellite-a",
            self.event_id,
        )
        await self.hass.async_block_till_done()


@pytest.fixture
async def wire(hass, socket_enabled, monkeypatch):
    harness = SpeakerWire(hass, monkeypatch)
    app = web.Application()
    app.router.add_get("/health", harness.health)
    app.router.add_post("/v1/turns", harness.accepted)
    app.router.add_post("/v1/event", harness.accepted)
    app.router.add_get("/v1/audio", harness.stream)
    app.router.add_get("/v1/events", harness.events)

    @asynccontextmanager
    async def opened():
        async with (
            TestServer(app) as server,
            AssistLiveClient(str(server.make_url("")), "test-only") as client,
        ):
            harness.client = client
            harness.entry = MockConfigEntry(
                domain="assist_live", data={"satellites": {}, "local_only": True}
            )
            harness.runtime = Runtime(hass, harness.entry, client)
            harness.runtime.ledger.clock = hass.loop.time
            harness.runtime.ledger.save = None
            hass.data["assist_live"] = {harness.entry.entry_id: harness.runtime}
            try:
                yield harness
            finally:
                for entity in harness.entities:
                    await entity.async_will_remove_from_hass()
                for task in harness.runtime.tasks:
                    task.cancel()
                await asyncio.gather(*harness.runtime.tasks, return_exceptions=True)
                for queue in harness.queues.values():
                    queue.put_nowait(None)
                harness.event_queue.put_nowait(None)

    async with opened():
        yield harness


@pytest.mark.parametrize("delay", [1, 5, 10])
async def test_delayed_cloud_result_raises_repair(wire, delay):
    turn_id = await wire.start()
    assert wire.seen == []
    await wire.advance(delay)
    assert wire.runtime.ledger.speaker_missing == 0
    assert turn_id in wire.runtime.ledger.speaker_reads
    await wire.result(turn_id)
    assert wire.issue() is not None
    assert wire.runtime.speaker_policy_violations == 1
    assert wire.seen[0]["turn_id"] == turn_id
    assert wire.runtime.ledger.records[turn_id].speaker["destination"] == "cloud"


async def test_heartbeats_do_not_extend_speaker_deadline(wire):
    turn_id = await wire.start()
    task = wire.runtime.ledger.speaker_reads[turn_id].task
    await wire.advance(10.5)
    assert turn_id in wire.runtime.ledger.speaker_reads
    await wire.advance(0.2)
    wire.queues[turn_id].put_nowait(
        encode_control(
            "speaker_result",
            turn_id,
            name="too-late",
            distance=0.25,
            destination="cloud",
        )
    )
    await asyncio.wait_for(asyncio.shield(task), 1)
    await wire.hass.async_block_till_done()
    assert wire.runtime.ledger.speaker_missing == 1
    assert turn_id not in wire.runtime.ledger.speaker_reads
    assert wire.issue() is None
    assert wire.runtime.speaker_policy_violations == 0
    assert wire.seen == []


@pytest.mark.parametrize("preexisting_issue", [False, True])
async def test_empty_name_is_received_but_not_a_destination(wire, preexisting_issue):
    if preexisting_issue:
        wire.runtime.speaker_metadata({"destination": "cloud"})
        wire.seen.clear()
    turn_id = await wire.start()
    await wire.result(turn_id, name="", destination="local")
    await wire.timing_result(wire.timing(turn_id, name="", destination="local"))
    diagnostics = await async_get_config_entry_diagnostics(wire.hass, wire.entry)
    assert not any(
        row["part"] == "speaker" for row in diagnostics["turns"][0]["destinations"]
    )
    assert wire.seen == []
    assert bool(wire.issue()) is preexisting_issue
    assert diagnostics["speaker_not_identified"] == 1
    assert wire.runtime.ledger.speaker_missing == 0


async def test_turn_end_does_not_cancel_pending_result(wire):
    turn_id = await wire.start()
    wire.runtime.ledger.device_event("satellite-a", turn_id, "playback_end")
    await wire.advance(1)
    assert turn_id in wire.runtime.ledger.speaker_reads
    await wire.result(turn_id)
    assert wire.issue() is not None
    assert wire.runtime.ledger.records[turn_id].speaker["name"] == "synthetic-speaker"


async def test_evicted_turn_still_applies_policy_and_fires_event(wire):
    turn_id = await wire.start()
    wire.runtime.ledger.device_event("satellite-a", turn_id, "playback_end")
    await wire.advance(10.1)
    wire.runtime.ledger.prune()
    assert turn_id not in wire.runtime.ledger.records
    assert turn_id in wire.runtime.ledger.speaker_reads
    await wire.result(turn_id)
    assert wire.issue() is not None
    assert wire.runtime.speaker_policy_violations == 1
    assert wire.seen[0]["turn_id"] == turn_id
    assert wire.seen[0]["satellite"] == "satellite-a"


async def test_different_satellites_keep_their_own_results(wire):
    first = await wire.start()
    second = await wire.start("satellite-b")
    await wire.result(second, name="speaker-b", destination="local")
    await wire.result(first, name="speaker-a")
    assert wire.runtime.ledger.records[first].speaker["name"] == "speaker-a"
    assert wire.runtime.ledger.records[second].speaker["name"] == "speaker-b"
    assert [(event["turn_id"], event["satellite"]) for event in wire.seen] == [
        (second, "satellite-b"),
        (first, "satellite-a"),
    ]


async def test_mismatched_wire_turn_cannot_attach_to_pending_turn(wire):
    turn_id = await wire.start()
    task = wire.runtime.ledger.speaker_reads[turn_id].task
    wire.queues[turn_id].put_nowait(
        encode_control(
            "speaker_result",
            str(uuid7()),
            name="wrong-turn",
            distance=0.25,
            destination="cloud",
        )
    )
    await task
    assert wire.runtime.ledger.speaker_missing == 1
    assert wire.runtime.ledger.records[turn_id].speaker is None
    assert wire.issue() is None
    assert wire.seen == []


async def test_unload_cancels_read_without_missing_count(wire):
    turn_id = await wire.start()
    await wire.entities[0].async_will_remove_from_hass()
    assert turn_id not in wire.runtime.ledger.speaker_reads
    assert wire.runtime.ledger.speaker_missing == 0
    assert wire.issue() is None


async def test_next_turn_supersedes_same_satellite_read(wire):
    first = await wire.start()
    second = await wire.start()
    assert first not in wire.runtime.ledger.speaker_reads
    assert second in wire.runtime.ledger.speaker_reads
    assert wire.runtime.ledger.speaker_superseded == 1
    assert wire.runtime.ledger.speaker_missing == 0
    diagnostics = await async_get_config_entry_diagnostics(wire.hass, wire.entry)
    assert diagnostics["speaker_superseded"] == 1
    await wire.result(second)
    assert [event["turn_id"] for event in wire.seen] == [second]


async def test_ninth_satellite_supersedes_oldest_read(wire):
    turns = [await wire.start(f"satellite-{index}") for index in range(9)]
    assert set(wire.runtime.ledger.speaker_reads) == set(turns[1:])
    assert wire.runtime.ledger.speaker_superseded == 1
    assert wire.runtime.ledger.speaker_missing == 0
    diagnostics = await async_get_config_entry_diagnostics(wire.hass, wire.entry)
    assert diagnostics["speaker_superseded"] == 1
    await wire.result(turns[-1])
    assert wire.seen[0]["turn_id"] == turns[-1]


async def test_concurrent_turns_keep_total_read_bound(wire):
    turns = [await wire.start(f"satellite-{index}") for index in range(8)]
    newest = await asyncio.gather(wire.start("satellite-8"), wire.start("satellite-9"))
    assert len(wire.runtime.ledger.speaker_reads) == 8
    assert set(wire.runtime.ledger.speaker_reads) == set(turns[2:] + newest)
    assert wire.runtime.ledger.speaker_superseded == 2
    assert wire.runtime.ledger.speaker_missing == 0


async def test_timing_result_repairs_closed_audio_and_is_idempotent(wire):
    turn_id = await wire.start()
    await wire.entities[0].async_will_remove_from_hass()
    event = wire.timing(turn_id)
    await wire.timing_result(event)
    await wire.timing_result(event)
    await wire.hass.async_block_till_done()
    assert wire.issue() is not None
    assert wire.runtime.speaker_policy_violations == 1
    assert len(wire.seen) == 1


@pytest.mark.parametrize("timing_first", [False, True])
async def test_audio_and_timing_result_apply_policy_once(wire, timing_first):
    turn_id = await wire.start()
    if timing_first:
        await wire.timing_result(wire.timing(turn_id))
    await wire.result(turn_id)
    if not timing_first:
        await wire.timing_result(wire.timing(turn_id))
    assert wire.issue() is not None
    assert wire.runtime.speaker_policy_violations == 1
    assert len(wire.seen) == 1


async def test_no_identification_on_timing_stream_has_no_destination(wire):
    turn_id = await wire.start()
    await wire.entities[0].async_will_remove_from_hass()
    await wire.timing_result(wire.timing(turn_id, name="", destination="local"))
    diagnostics = await async_get_config_entry_diagnostics(wire.hass, wire.entry)
    assert diagnostics["speaker_not_identified"] == 1
    assert not any(
        row["part"] == "speaker" for row in diagnostics["turns"][0]["destinations"]
    )
    assert wire.runtime.ledger.speaker_missing == 0
    assert wire.issue() is None
    assert wire.seen == []


@pytest.mark.parametrize("local_only", [False, True])
@pytest.mark.parametrize(
    "channel,destination,absent",
    [
        ("audio", "local", False),
        ("audio", "cloud", False),
        ("timing", "local", False),
        ("timing", "local", True),
        ("timing", "cloud", False),
        ("timing", "cloud", True),
    ],
)
async def test_unnamed_result_keeps_only_checked_cloud_destination(
    wire, local_only, channel, destination, absent
):
    wire.runtime.ledger.local_only = local_only
    turn_id = await wire.start()
    if channel == "audio":
        await wire.result(turn_id, name="", destination=destination)
    else:
        await wire.entities[0].async_will_remove_from_hass()
        event = wire.timing(turn_id, name="", destination=destination)
        if not absent:
            event["data"]["name"] = ""
        await wire.timing_result(event)
    record = wire.runtime.ledger.records[turn_id]
    speaker_rows = [row for row in record.destinations if row["part"] == "speaker"]
    assert speaker_rows == (
        [{"part": "speaker", "provider": "speaker_plugin", "destination": "cloud"}]
        if destination == "cloud"
        else []
    )
    assert bool(wire.issue()) is (local_only and destination == "cloud")
    assert wire.runtime.speaker_policy_violations == (
        local_only and destination == "cloud"
    )
    assert wire.runtime.ledger.speaker_not_identified == (destination == "local")
    assert wire.runtime.ledger.speaker_missing == 0
    assert wire.seen == []


async def test_wrong_satellite_result_does_not_attach_or_apply_policy(wire):
    turn_id = await wire.start()
    wire.runtime.ledger.speaker_result(
        turn_id, wire.timing(turn_id)["data"], satellite="satellite-b"
    )
    await wire.hass.async_block_till_done()
    record = wire.runtime.ledger.records[turn_id]
    assert record.speaker is None
    assert not any(row["part"] == "speaker" for row in record.destinations)
    assert wire.runtime.speaker_policy_violations == 0
    assert wire.issue() is None
    assert wire.seen == []


async def test_wrong_satellite_timing_does_not_attach_or_apply_policy(wire):
    turn_id = await wire.start()
    event = wire.timing(turn_id)
    event["satellite"] = "satellite-b"
    wire.runtime.ledger.observe(event)
    await wire.hass.async_block_till_done()
    record = wire.runtime.ledger.records[turn_id]
    assert record.speaker is None
    assert "speaker_result" not in record.timings
    assert "speaker_result" not in record.stages
    assert not any(row["part"] == "speaker" for row in record.destinations)
    assert wire.runtime.speaker_policy_violations == 0
    assert wire.issue() is None
    assert wire.seen == []


async def test_speaker_timing_keeps_stage_and_timestamp(wire):
    turn_id = await wire.start()
    await wire.timing_result(wire.timing(turn_id))
    record = wire.runtime.ledger.records[turn_id]
    assert "speaker_result" in record.timings
    assert record.timings["speaker_result"]["t_utc_ns"] == 1
    assert "speaker_result" in record.stages
