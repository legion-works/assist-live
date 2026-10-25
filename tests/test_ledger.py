from uuid import UUID


def test_voice_overlap_announcement_followup_and_idle(ledger):
    a = ledger.open_voice("satellite-a")
    b = ledger.open_voice("satellite-b")
    assert UUID(a.turn_id).version == 7
    assert a.conversation_id is None
    ledger.satellite_state("satellite-a", "idle")
    assert ledger.current("satellite-a") == a
    ledger.device_event("satellite-a", b.turn_id, "playback_end")
    assert ledger.current("satellite-b") == b
    assert ledger.current("satellite-a") == a
    ledger.device_event("satellite-b", b.turn_id, "wake_detected")
    ledger.device_event("satellite-a", a.turn_id, "playback_end")
    assert ledger.current("satellite-a") is None
    assert ledger.current("satellite-b") == b
    followup = ledger.open_voice("satellite-a")
    assert followup.parent_turn_id == a.turn_id
    announcement = ledger.open_tts("satellite-a")
    assert announcement.turn_type == "tts_only"
    assert ledger.records[followup.turn_id].status == "incomplete"
    voice = ledger.open_voice("satellite-a")
    ledger.mark(voice.turn_id, "stt_final")
    assert ledger.open_tts("satellite-a") == voice
    ledger.open_voice("satellite-a")
    assert ledger.records[voice.turn_id].status == "incomplete"


def test_deadline_and_parent_window(client):
    from custom_components.assist_live.ledger import TurnLedger

    now = [0.0]
    ledger = TurnLedger(client, clock=lambda: now[0])
    first = ledger.open_voice("satellite-a")
    now[0] = 119.999
    assert ledger.current("satellite-a") == first
    now[0] = 120
    assert ledger.current("satellite-a") is None
    assert ledger.records[first.turn_id].status == "incomplete"
    now[0] = 131
    assert ledger.open_voice("satellite-a").parent_turn_id is None


async def test_connect_timeout_and_restart(ledger, client):
    from custom_components.assist_live.ledger import TurnLedger

    turn = ledger.open_voice("satellite-a")
    assert await ledger.connect(turn)
    client.open_turn.assert_awaited_once_with(turn)
    saved = ledger.snapshot()
    restarted = TurnLedger(client)
    await restarted.restore(saved)
    assert restarted.current("satellite-a") is None
    event = client.event.call_args.args[0]
    assert event.turn_id == turn.turn_id
    assert event.stage == "turn_superseded"
    assert event.data == {"reason": "restart"}


async def test_connection_deadline(ledger, client, monkeypatch):
    import asyncio

    deadlines = []
    real_timeout = asyncio.timeout

    def timeout(seconds):
        deadlines.append(seconds)
        return real_timeout(0)

    monkeypatch.setattr("custom_components.assist_live.ledger.asyncio.timeout", timeout)

    async def blocked(_):
        await asyncio.Event().wait()

    client.open_turn.side_effect = blocked
    assert not await ledger.connect(ledger.open_voice("satellite-a"))
    assert deadlines == [10]
