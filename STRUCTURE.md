# Repository structure

| Path | Purpose |
|---|---|
| `custom_components/assist_live/` | HA package, config flow, manifest, and English strings |
| `custom_components/assist_live/stt.py`, `tts.py` | Voice forwarding and standard-engine fallback |
| `custom_components/assist_live/stt_fallback.py` | Bounded turn audio, replay, and local voice detection |
| `custom_components/assist_live/tts_fallback.py` | WAV format checks and PCM extraction |
| `custom_components/assist_live/diagnostics.py` | Content-free failure counts and fallback mode |
| `custom_components/assist_live/sensor.py` | Last-turn latency, quality, source, and uncertainty for each satellite |
| `docs/configuration.md`, `docs/privacy.md` | Modes, local-only limits, transcript policy, and diagnostics |
| `tests/test_modes.py`, `test_diagnostics.py`, `test_sensor.py` | Destination policy, metadata, redaction, and timing tests |
| `tools/check_release_pin.py`, `tests/test_release_pin.py` | Client version and PyPI publication check |
| `custom_components/assist_live/ledger.py`, `repairs.py` | Per-satellite turns and repair issues |
| `docs/setup.md` | Satellite bindings, pipelines, voices, and fallback limits |
| `tests/test_stt_forward.py`, `test_tts_forward.py`, `test_ledger.py` | Forwarding, cancellation, playback separation, and turn rules |
| `tests/test_stt_fallback.py` | Server-message timers, failover, replay, and deadline checks |
| `tests/test_tts_stream.py`, `test_tts_fallback.py` | Stream order, text replay, format agreement, and failure notifications |
| `docs/troubleshooting/tts.md` | TTS fallback rules and notification settings |
| `docs/troubleshooting/stt.md` | STT fallback limits and failure checks |
| `tests/test_config_flow.py`, `test_repairs.py`, `test_tts_lookup_contract.py` | HA runtime setup, repair issues, and TTS lookup contract |
| `hacs.json` | HACS package metadata |
| `docs/design.md` | Public design and phase gates |
| `docs/research/contracts.md` | Upstream contracts and prototype limits |
| `docs/verification.md` | Commands and gate results |
| `tests/test_package.py` | Package boundary test |
| `tests/test_public_tree.py` | Bootstrap and privacy checker tests |
| `tools/check_bootstrap.py` | Package and document checks |
| `tools/check_public_tree.py` | Worktree and reachable-history checks |
| `tools/corpus.py`, `tests/test_corpus.py` | Corpus source pins, records, fetch limits, and checks |
| `tools/annotate_corpus.py`, `tests/test_annotate_corpus.py` | Local boundary marks, checked import, and calibration statistics |
| `tools/test_environment.py`, `tests/test_test_environment.py` | Virtual intent coverage, reply groups, and operation records |
| `tests/fixtures/custom_components/assist_live_test_agent/` | Test-only HA conversation agent and native timer receiver |
| `tests/fixtures/ha-virtual.yaml`, `tests/fixtures/intents.json` | Isolated virtual entities and intent inventory |
| `docs/testing/virtual-intents.md` | Virtual test installation, records, and limits |
| `docs/testing/corpus.md`, `docs/testing/recording-kit.md` | Corpus contract, boundary procedure, and volunteer recording kit |
| `pyproject.toml`, `uv.lock`, `.python-version` | Python metadata and test dependencies |
| Root Markdown files | Project status, architecture, contribution rules, and vulnerability reports |
| `LICENSE` | Full Apache-2.0 license |

The HA app and server are not in this repository.
The ESPHome component is in a different GPL-3.0 repository.
