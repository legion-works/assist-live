# Repository structure

| Path | Purpose |
|---|---|
| `custom_components/assist_live/` | HA package, config flow, manifest, and English strings |
| `custom_components/assist_live/stt.py`, `tts.py` | Voice forwarding and standard-engine fallback |
| `custom_components/assist_live/stt_fallback.py` | Bounded turn audio, replay, and local voice detection |
| `custom_components/assist_live/tts_fallback.py` | WAV format checks and PCM extraction |
| `custom_components/assist_live/diagnostics.py` | Content-free failure counts and fallback mode |
| `custom_components/assist_live/stt_hints.py`, `tests/test_stt_hints.py`, `test_stt_hints_contract.py` | HA Assist names, change subscriptions, and HA interface contracts |
| `custom_components/assist_live/llm.py`, `tests/test_llm.py` | Spoken-reply rules for the HA Assist API |
| `custom_components/assist_live/sensor.py` | Last-turn latency, quality, source, and uncertainty for each satellite |
| `custom_components/assist_live/entity.py` | Base entity: entity names and the satellite device connection |
| `custom_components/assist_live/quality_scale.yaml` | HA quality scale rules and their status |
| `docs/configuration.md`, `docs/privacy.md` | Modes, local-only limits, transcript policy, and diagnostics |
| `tests/test_modes.py`, `test_diagnostics.py`, `test_sensor.py` | Destination policy, metadata, redaction, and timing tests |
| `tools/check_release_pin.py`, `tests/test_release_pin.py` | Client version and PyPI publication check |
| `custom_components/assist_live/ledger.py`, `repairs.py` | Per-satellite turns and repair issues |
| `docs/setup.md` | Satellite bindings, pipelines, voices, and fallback limits |
| `tests/test_stt_forward.py`, `test_tts_forward.py`, `test_ledger.py` | Forwarding, cancellation, playback separation, and turn rules |
| `tests/test_stt_fallback.py` | Server-message timers, failover, replay, and deadline checks |
| `tests/test_tts_stream.py`, `test_tts_fallback.py` | Stream order, text replay, format agreement, and failure notifications |
| `tests/test_tts_stream_conversion.py` | Streamed WAV conversion through HA and FFmpeg |
| `tests/test_fallback_lifecycle.py`, `test_options_fallback.py` | Engine reloads, stored metadata, and fallback selection changes |
| `docs/troubleshooting/tts.md` | TTS fallback rules and notification settings |
| `docs/troubleshooting/stt.md` | STT fallback limits and failure checks |
| `tests/test_config_flow.py`, `test_repairs.py`, `test_tts_lookup_contract.py` | HA runtime setup, repair issues, and TTS lookup contract |
| `hacs.json` | HACS package metadata |
| `docs/design.md` | Public design and phase gates |
| `docs/research/contracts.md` | Upstream contracts and prototype limits |
| `docs/verification.md` | Commands and gate results |
| `tools/check_packaging.py`, `tests/test_package.py` | Package metadata, archive extraction, and import checks |
| `tools/check_public_tree.py`, `tests/test_public_tree.py` | Working tree and Git history checks |
| `tools/check_ci.py`, `tests/test_ci.py` | Release publisher and CI result checks |
| `tools/corpus.py`, `tests/test_corpus.py` | Corpus source pins, records, fetch limits, and checks |
| `tools/annotate_corpus.py`, `tests/test_annotate_corpus.py` | Local boundary marks, checked import, and calibration statistics |
| `tools/test_environment.py`, `tests/test_test_environment.py` | Virtual intent coverage, reply groups, and operation records |
| `tests/fixtures/custom_components/assist_live_test_agent/` | Test-only HA conversation agent and native timer receiver |
| `tests/fixtures/ha-virtual.yaml`, `tests/fixtures/intents.json` | Isolated virtual entities and intent inventory |
| `docs/testing/virtual-intents.md` | Virtual test installation, records, and limits |
| `docs/testing/corpus.md`, `docs/testing/recording-kit.md` | Corpus contract, boundary procedure, and volunteer recording kit |
| `tools/correct_transcripts.py`, `tools/timer_selection.py` | Correct transcripts, listening review, and held-out recording replacements |
| `tools/device_replay.py`, `tools/calibrate_playback.py` | Device replay and acoustic playback calibration |
| `tools/fault_runner.py`, `tools/fault_hardware.py` | Simulated and hardware fault trials with same-engine baselines |
| `tools/report.py`, `tools/measurement.py`, `tools/metrics.py`, `tools/result_text.py` | Acceptance reports, timing, and text rules |
| `tools/native_matrix.py`, `docs/testing/native-functions.md` | Native-function observations and report procedures |
| `docs/testing/fault-runner.md`, `docs/testing/measurement-rules.md` | Fault qualification, timing, missing events, and uncertainty rules |
| `pyproject.toml`, `uv.lock`, `.python-version` | Python metadata and test dependencies |
| Root Markdown files | Project status, architecture, contribution rules, and vulnerability reports |
| `LICENSE` | Full Apache-2.0 license |

The HA app and server are not in this repository.
The ESPHome component is in a different GPL-3.0 repository.
