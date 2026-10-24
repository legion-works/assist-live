# Architecture

## Current boundary

The `assist_live` package has no import side effects.
Its config flow aborts with `bootstrap_only` and does not make a config entry.
No runtime service, entity, platform, client, or app is part of this bootstrap.

## Intended components

The integration connects HA Assist to the server in a different repository through a versioned WebSocket protocol.
Each satellite has its own STT entity, TTS entity, and pipeline.
The integration keeps the turn ledger and the late-result service.
The server supplies turn detection, voice providers, and timing storage.
The ESPHome component sends timing events without replacing the native voice assistant.

See [the frozen design](docs/design.md) for limits, fallback behavior, and phase gates.

## Public data boundary

The repository contains no household recordings, personal data, or secrets.
The public-tree checker examines publishable worktree files, reachable blobs, paths, and commit metadata.
The operator keeps its private denylist external to Git.
The checker prints counts and object identifiers, not private text.
It refuses unknown binary files and audio without an approved SHA-256 hash.
