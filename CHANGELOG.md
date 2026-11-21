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

### Corrections

- Find fallback engines again after reload and keep their last known metadata through outages and HA restarts.
- Send timing events in the background so an unavailable record server does not make a voice result wait.
- Show current server health apart from last known values, and bound speaker-result reads. Transcripts do not wait for these reads.
- Refresh STT name hints after changes to device names, areas, or parent devices, and after server restarts.

No release is available. Version `0.0.0` is a metadata placeholder.
