# assist-live

Realtime voice work on the native Home Assistant Assist pipeline.

**Experimental. No release is available.** The integration supplies STT, TTS, and latency sensor entities for each satellite.
It forwards voice through the server and uses standard HA engines for fallback.
A published client package is necessary for installation through HA. See [setup](docs/setup.md).

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
task check
task package-check
```

Use an isolated HA test instance, not a live household instance.
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
