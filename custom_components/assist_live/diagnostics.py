import re
from datetime import datetime
from typing import Any

from homeassistant.components import assist_pipeline
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .config_flow import destination, entity_iot_class
from .const import DOMAIN
from .tts import AssistLiveTTS


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
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    runtime = hass.data[DOMAIN][entry.entry_id]
    streaming = full_file = 0
    for entity in runtime.tts.values():
        if isinstance(entity, AssistLiveTTS):
            if entity.fallback.async_supports_streaming_input():
                streaming += 1
            else:
                full_file += 1
    settings = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
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
    if settings.get("pipelines"):
        pipelines = {p.id: p for p in assist_pipeline.async_get_pipelines(hass)}
        for pipeline_id in settings["pipelines"].values():
            if pipeline := pipelines.get(pipeline_id):
                value = await entity_iot_class(hass, pipeline.conversation_engine)
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
                    }
                    for row in record.destinations
                ],
                "voice": safe_label(record.voice) if record.voice else None,
                "audio_format": safe_format(record.audio_format),
                "tts_text_input": "sentence_buffered"
                if any(row["provider"] == "piper" for row in record.destinations)
                else "unknown",
                "tts_audio_output": "streamed"
                if record.audio_format is not None
                else "unknown",
            }
        )
    health = runtime.ledger.last_health
    return {
        "protocol_version": 1,
        "tts_fallback": {"streaming": streaming, "full_file": full_file},
        "failures": dict(runtime.ledger.failures),
        "late_server_failures": runtime.ledger.late_server_failures,
        "fallback_count": runtime.ledger.failures.get("fallback_used", 0),
        "speaker_metadata_missing": runtime.ledger.speaker_missing,
        "speaker_policy_violations": getattr(runtime, "speaker_policy_violations", 0),
        "server_local_only": health.local_only
        if health is not None and health.local_only is not None
        else "not_reported",
        "server_destinations": {
            part: value
            for part, value in health.destinations.items()
            if part in {"stt", "tts", "speaker"}
            and value in {"local", "cloud", "unknown"}
        }
        if health is not None and health.destinations is not None
        else "not_reported",
        "local_only": settings.get("local_only", False),
        "record_transcripts": settings.get("record_transcripts", False),
        "fallbacks": fallbacks,
        "local_decisions": decisions,
        "agent_boundary": "outside_local_only",
        "agents": agents,
        "turns": turns,
    }
