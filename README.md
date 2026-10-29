# assist-live

Realtime voice work on the native Home Assistant Assist pipeline.

**Experimental. No release is available.** This repository contains the package boundary and design documents only.
It does not supply STT entities, TTS entities, or a working voice pipeline.
The config flow refuses setup until the runtime is available.

## Purpose

The design adds turn detection, streamed voice, and timing records to HA Assist.
The native satellite keeps timers, announcements, mute, and the selected conversation agent.
These are design goals, not compatibility or performance claims.

## Development

Use Python `3.14.2` or a subsequent version and `uv`.
The test matrix uses HA `2026.10.1` (minimum) and `2026.10.4` (current).
Run these commands from the repository root:

```sh
uv sync --locked
uv run pytest tests/test_package.py tests/test_public_tree.py -q
uv run python tools/check_bootstrap.py
```

Do not install this bootstrap package in a live HA instance.
HACS metadata gives the intended package layout, not an available release.
The server and the HA app belong in the different `assist-live-server` repository.

## Documentation

- [Architecture](ARCHITECTURE.md)
- [Repository structure](STRUCTURE.md)
- [Design and phase gates](docs/design.md)
- [Upstream contracts](docs/research/contracts.md)
- [Verification record](docs/verification.md)
- [Contribution instructions](CONTRIBUTING.md)
- [Vulnerability reports](SECURITY.md)

## License

Apache-2.0. See [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).

A Legion Works project.
