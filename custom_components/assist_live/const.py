from assist_live_client.protocol import MESSAGE_DEADLINE

DOMAIN = "assist_live"
CONF_NOTIFY_TTS_FAILURE = "notify_tts_failure"

# The server sends final before identification. At server revision f6ac2d6,
# destinations.py:78 permits 5 s for resolution and speaker.py:98 permits 5 s
# for HTTP. Heartbeats must not extend this total deadline.
SPEAKER_RESOLVE_TIMEOUT = 5.0
SPEAKER_HTTP_TIMEOUT = 5.0
SPEAKER_RESULT_DEADLINE = (
    SPEAKER_RESOLVE_TIMEOUT + SPEAKER_HTTP_TIMEOUT + MESSAGE_DEADLINE
)
# One pending read per satellite prevents accumulation during rapid turns.
# Eight reads bound pending audio sockets, not live audio or event subscriptions.
# The server applies its own limits (revision f6ac2d6, app.py:50-51).
MAX_SPEAKER_READS_PER_SATELLITE = 1
MAX_SPEAKER_READS = 8
