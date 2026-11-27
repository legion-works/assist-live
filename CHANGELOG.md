# Changelog

## Unreleased

- Add the experimental HA package boundary.
- Add bootstrap and public-tree checks with tests.
- Record the design and upstream contracts.

### Added

- Add STT and TTS forwarding entities and a pipeline for each satellite.
- Add bounded STT fallback with buffered audio replay and local voice detection.
- Stream reply text and audio, with TTS fallback from a recorded sentence position when audio formats agree.
- Show server voices in HA and select fallback engines and their TTS voice through the integration options.
- Send Assist entity names, aliases, and area names to servers that report local STT name hints.
- Give spoken-reply rules to conversation agents that use the HA Assist API.
- Add local-only destination checks, repair issues, privacy-safe diagnostics, and a latency sensor for each satellite.
- Add turn records with parent turns, fallback causes, server-audio sample counts, and HA receipt times.
- Add corpus import, voice-boundary marks, correct-transcript review, device replay, playback calibration, and native-function reports.
- Add simulated and hardware fault runners with recording qualification, same-engine baselines, bounded replacements, and cleanup checks.
- Add the HA quality scale file, field descriptions in the config and options steps, and installation and removal steps.

### Changes

- The config flow stops a second entry with the same server URL and token. The check ignores the case of the scheme and host, a trailing slash, and a default port. It also stops a second flow that started before the first flow made its entry. Different tokens on one server stay different entries.
- Diagnostics of an entry that is not loaded give the entry state with redacted data, not an error.
- The STT, TTS, and latency sensor entities attach to the device of their satellite. The entity IDs in the registry do not change. The displayed names change:
  - STT: `assist-live <key>` becomes `<satellite device name> STT`.
  - TTS: `assist-live <key>` becomes `<satellite device name> TTS`.
  - Latency sensor: `assist-live <key> last turn latency` becomes `<satellite device name> Last turn latency`.
- Setup removes an assist-live device that has no entities and no satellite connection. The satellite's own device does not change.

### Corrections

- Start slow 22050 Hz replies with 250 ms of silent audio. From HA 2026.11, the converter can give FLAC to the satellite before the server audio. The HA 2026.10 converter buffers the silent audio.
- Find fallback engines again after reload and keep their last known metadata through outages and HA restarts.
- Send timing events in the background so an unavailable record server does not make a voice result wait.
- Show current server health apart from last known values, and bound speaker-result reads. Transcripts do not wait for these reads.
- Refresh STT name hints after changes to device names, areas, or parent devices, and after server restarts.
- Refuse WebSocket redirects in the pipeline probe and send credentials only to the checked address.
- Refuse redirects in the test tools that send credentials to the isolated HA instance.
- Keep the entry loadable when the integration cannot write the turn store as HA stops the entry. The turn state stays in memory until the next good write. An HA restart clears it.
- Do not open an older pending turn again after a newer turn of the same satellite opened.

No release is available. Version `0.0.0` is a metadata placeholder.
