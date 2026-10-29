from assist_live_client.protocol import MESSAGE_DEADLINE as MESSAGE_DEADLINE

DOMAIN = "assist_live"
CONF_NOTIFY_TTS_FAILURE = "notify_tts_failure"
CONF_FALLBACK_TTS_VOICE = "fallback_tts_voice"

# The server sends final before identification. At server revision b3b1e16,
# destinations.py permits 5 s for resolution and speaker.py permits 5 s
# for HTTP. Heartbeats must not extend this total deadline.
SPEAKER_RESOLVE_TIMEOUT = 5.0
SPEAKER_HTTP_TIMEOUT = 5.0
SPEAKER_RESULT_DEADLINE = (
    SPEAKER_RESOLVE_TIMEOUT + SPEAKER_HTTP_TIMEOUT + MESSAGE_DEADLINE
)
# One pending read per satellite prevents accumulation during rapid turns.
# Eight reads bound pending audio sockets, not live audio or event subscriptions.
# The server applies its own limits (revision b3b1e16, app.py).
MAX_SPEAKER_READS_PER_SATELLITE = 1
MAX_SPEAKER_READS = 8
# The pinned server uses this normalized distance bound (b3b1e16, speaker.py:40).
MAX_SPEAKER_DISTANCE = 2
