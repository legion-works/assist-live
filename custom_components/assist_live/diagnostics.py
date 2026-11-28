import re
from datetime import datetime
from typing import Any

from homeassistant.components import assist_pipeline
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import AssistLiveConfigEntry
from .config_flow import destination, entity_iot_class
from .routing import selected_pipelines
from .settings import effective_settings
from .tts import AssistLiveTTS, fallback_engine

# The loaded output also has no address, token, entity ID, or device ID.
# The voices and pipelines keys use device IDs.
ENTRY_REDACT = {
    "fallback_stt",
    "fallback_tts",
    "fallback_tts_voice",
    "local_decisions",
    "pipeline_selects",
    "pipelines",
    "satellites",
    "token",
    "url",
    "voices",
}


def safe_label(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
        return value
    return "[redacted]"


def safe_timestamp(value: object) -> str | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.isoformat() if parsed.tzinfo is not None else None
    except ValueError:
        return None


def safe_format(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "codec": value.get("codec") if value.get("codec") == "pcm_s16le" else "unknown",
        **{
            key: value[key]
            for key in ("rate", "channels", "width")
            if type(value.get(key)) is int
        },
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AssistLiveConfigEntry
) -> dict[str, Any]:
    # HA calls this handler also for an entry that is not loaded. A release
    # of the entry or a setup error removes runtime_data.
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return {
            "loaded": False,
            "entry": {
                "data": async_redact_data(dict(entry.data), ENTRY_REDACT),
                "options": async_redact_data(dict(entry.options), ENTRY_REDACT),
            },
        }
    streaming = full_file = 0
    for entity in runtime.tts.values():
        if isinstance(entity, AssistLiveTTS):
            try:
                engine = fallback_engine(hass, entity.fallback_entity_id)
            except HomeAssistantError:
                continue
            if not engine.available:
                continue
            if engine.async_supports_streaming_input():
                streaming += 1
            else:
                full_file += 1
    settings = effective_settings(entry)
    fallbacks = {}
    decisions = []
    for part in ("stt", "tts"):
        entity_id = settings.get(f"fallback_{part}", "")
        iot_class = await entity_iot_class(hass, entity_id) if entity_id else None
        mode = destination(iot_class)
        decision = settings.get("local_decisions", {}).get(entity_id)
        if mode == "unknown" and decision and decision.get("destination") == "local":
            mode = "local"
            decisions.append(
                {
                    "part": part,
                    "destination": "local",
                    "recorded_at": safe_timestamp(decision.get("recorded_at")),
                }
            )
        fallbacks[part] = {
            "destination": mode,
            "iot_class": safe_label(iot_class) if iot_class else None,
        }
    agents = []
    selectors = settings.get("pipeline_selects", {})
    selected = (
        selected_pipelines(
            hass, selectors.values(), assist_pipeline.async_get_pipelines(hass)
        )
        if selectors
        else {}
    )
    for device in settings.get("satellites", {}):
        pipeline = selected.get(selectors.get(device))
        value = (
            await entity_iot_class(hass, pipeline.conversation_engine)
            if pipeline is not None
            else None
        )
        agents.append(
            {
                "iot_class": safe_label(value) if value else None,
                "destination": destination(value),
            }
        )
    turns = []
    for record in runtime.ledger.latest.values():
        turns.append(
            {
                "turn_id": record.turn.turn_id,
                "turn_type": record.turn.turn_type,
                "opened_utc_ns": record.turn.opened_utc_ns,
                **(
                    {
                        "server_audio_samples": record.server_audio_samples,
                        "server_audio_last_chunk_samples": (
                            record.server_audio_last_chunk_samples
                        ),
                    }
                    if record.server_audio_samples is not None
                    else {}
                ),
                "satellite": safe_label(record.turn.satellite),
                "destinations": [
                    {
                        "part": safe_label(row["part"]),
                        "provider": "ha_entity"
                        if row["part"].endswith("_fallback")
                        else row["provider"]
                        if row["provider"]
                        in {
                            "parakeet",
                            "deepgram",
                            "piper",
                            "elevenlabs",
                            "server",
                            "speaker_plugin",
                            "unknown",
                            "injected",
                        }
                        else "[redacted]",
                        "destination": row["destination"]
                        if row["destination"] in {"local", "cloud", "unknown"}
                        else "unknown",
                        **(
                            {
                                "fallback_reason": record.fallback_reasons[
                                    row["part"].removesuffix("_fallback")
                                ]
                                if record.fallback_reasons[
                                    row["part"].removesuffix("_fallback")
                                ]
                                in runtime.ledger.fallback_reasons
                                else "unknown"
                            }
                            if row["part"].endswith("_fallback")
                            and row["part"].removesuffix("_fallback")
                            in record.fallback_reasons
                            else {}
                        ),
                    }
                    for row in record.destinations
                ],
                "voice": safe_label(record.voice) if record.voice else None,
                "audio_format": safe_format(record.audio_format),
                "reply_start": record.reply_start,
                "tts_text_input": "sentence_buffered"
                if any(row["provider"] == "piper" for row in record.destinations)
                else "unknown",
                "tts_audio_output": record.tts_audio_output,
            }
        )
    health = runtime.last_healthy_health
    last_known = (
        {
            "local_only": health.local_only
            if health.local_only is not None
            else "not_reported",
            "tts_voice_count": len(health.tts_voices),
            "tts_default_voice": safe_label(health.tts_default_voice)
            if health.tts_default_voice is not None
            else None,
            "destinations": {
                part: value
                for part, value in health.destinations.items()
                if part in {"stt", "tts", "speaker"}
                and value in {"local", "cloud", "unknown"}
            }
            if health.destinations is not None
            else "not_reported",
        }
        if health is not None
        else None
    )
    return {
        "protocol_version": 1,
        "tts_fallback": {"streaming": streaming, "full_file": full_file},
        "stt_hints": {
            (
                label
                if (label := safe_label(satellite)) != "[redacted]"
                else f"[redacted:{index}]"
            ): dict(row)
            for index, (satellite, row) in enumerate(runtime.hints.status.items(), 1)
        },
        "stt_hints_failures": runtime.hints.failures,
        "failures": dict(runtime.ledger.failures),
        "late_server_failures": runtime.ledger.late_server_failures,
        "early_reply_starts": runtime.ledger.early_reply_starts,
        "counters_since_utc_ns": runtime.ledger.counters_since_utc_ns,
        "timing_events_dropped": runtime.ledger.timing_events_dropped,
        "turn_opens_dropped": runtime.ledger.turn_opens_dropped,
        "fallback_count": runtime.ledger.failures.get("fallback_used", 0),
        "fallback_reasons": dict(runtime.ledger.fallback_reasons),
        "speaker_metadata_missing": runtime.ledger.speaker_missing,
        "speaker_not_identified": runtime.ledger.speaker_not_identified,
        "speaker_superseded": runtime.ledger.speaker_superseded,
        "speaker_policy_violations": runtime.speaker_policy_violations,
        "server_healthy": runtime.server_healthy,
        "server_last_healthy": runtime.last_healthy.isoformat()
        if runtime.last_healthy is not None
        else None,
        "server_last_known": last_known,
        "local_only": settings.get("local_only", False),
        "record_transcripts": settings.get("record_transcripts", False),
        "fallbacks": fallbacks,
        "local_decisions": decisions,
        "agent_boundary": "outside_local_only",
        "agents": agents,
        "turns": turns,
    }
