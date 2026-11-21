# Architecture

## Current boundary

The config flow connects satellite devices and pipeline selectors to provisioned server keys.
Config-entry setup starts the client, turn ledger, event subscriptions, and STT, TTS, and sensor platforms.
It also starts STT name hints. The `llm` platform supplies spoken-reply rules through the HA Assist API, without new tools.
The HA app is in the server repository, not this integration package.

## Components

The integration connects HA Assist to the server in a different repository through a versioned WebSocket protocol.
Each satellite has its own STT entity, TTS entity, and pipeline.
The integration keeps the turn ledger. P2 will add the late-result service.
The server supplies turn detection, voice providers, and timing storage.
The ESPHome component sends timing events without replacing the native voice assistant.

Timing events use a bounded background sender, so voice turns do not wait for event requests.
Fallback engines can start after assist-live. Each request finds the current engine by its entity ID.

See [the design](docs/design.md) for limits, fallback behavior, and phase gates.

## Public data boundary

The repository contains no household recordings, personal data, or secrets.
The public-tree checker examines publishable worktree files, reachable blobs, paths, and commit metadata.
The operator keeps its private denylist external to Git.
The checker prints counts and object identifiers, not private text.
It refuses unknown binary files and audio without an approved SHA-256 hash.
