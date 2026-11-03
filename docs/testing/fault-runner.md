# Fault runner records

The fault runner uses only the isolated test environment.
Synthetic tests do not prove hardware acceptance.
Get approval before a live bench run.

## Same-engine baselines

Before each fault, the runner collects a no-fault transcript for each complete recording and STT engine.
The server baseline uses the same pipeline and virtual device as the fault trials.
With `--temporary-agent-pipeline`, this is the same temporary pipeline, not its source pipeline.
The baseline run can operate the virtual targets through the test conversation agent.

The runner finds the assist-live config entry through the pipeline STT entity registry row.
It reads `fallback_stt` from the initial public `options/flow` response and deletes the flow in `finally` without a submission.

The fallback baseline uses a temporary, non-preferred copy of the trial pipeline with the selected fallback STT entity.
Only its name and STT entity change.
The runner deletes this copy on success, failure, or cancellation.
Before and after checks compare all pipelines, the preferred pipeline, and select states.
Cleanup uses a new authenticated connection, a timeout, and one retry.
A cleanup failure stops the run.

The baselines and trials use `assist_pipeline/run` and the same audio sender.
The request starts at STT, ends at intent, and uses `input: {sample_rate: 16000}` with the same virtual device.
The sender uses the same PCM bytes without a WAV header, in 640-byte frames with 20 ms timing.
Each frame has the HA binary-handler byte before the PCM payload.
The two paths add two seconds of silent PCM, then send only the handler byte to end the stream.
The paths do not add different audio chunks.

In HA `2026.10.1` and `2026.10.4`, `assist_pipeline/websocket_api.py:207-212` supplies the audio defaults from the run request.
The defaults are `noise_suppression_level=0`, `auto_gain_dbfs=0`, `volume_multiplier=1.0`, and `is_vad_enabled=True`.

`assist_pipeline/default_pipeline.py:163-170,1043-1049` selects the enhancer from these settings, not from the STT entity.
With these settings, `assist_pipeline/audio_enhancer.py:63-98` keeps the PCM unchanged and calculates `speech_probability` for VAD.

The STT entity controls external VAD segmentation through `requires_external_vad` (`assist_pipeline/default_pipeline.py:437-448,490-524`).
The assist-live entity uses `requires_external_vad=False`. The fallback entity can use `requires_external_vad=True`.
The two paths use the same input and enhancer, but stream termination at the engine can be different.
The fallback baseline has no assist-live code between HA and the engine.
A trial/baseline difference points at the assist-live fallback path.

Each trial keeps `stt_engines` and `baselines`, with the engine entity ID, `clip_id`, audio SHA-256, transcript, and baseline status.
The two baselines keep their pipeline ID, `interface: assist_pipeline/run`, and no-fault run records.
Each trial also names these paths in `baseline_paths`, also when the fault window is unavailable.
The fallback baseline keeps its cleanup checks in `pipeline_cleanup`.
The runner collects each recording one time and uses its baseline records for subsequent trials in that run.

The diagnostic turn row selects the transcript engine: an STT fallback cause selects `fallback`, and no STT fallback cause selects `server`.
A TTS fallback cause does not select the STT engine.
The classifier compares the trial transcript with that engine's baseline through `tools/result_text.py:words`.
The prompt text does not select fault acceptance.
Intent and device operations must agree with the corpus intent.

An empty HA result with complete records and no device operation stays satisfactory when its baseline is available.
A missing or unsatisfactory baseline gives `status: unavailable`, also for an empty result.
A different engine identity, `clip_id`, or audio SHA-256 also makes the baseline unavailable.

## Recorded causes

Each trial downloads public diagnostics for the pipeline STT config entry before and after the turn.
The runner keeps the `fallback_reasons` counts and their delta.
It records `server_container_stopped` in `injected_fault` and the integration cause in `failure_cause`.

The cause comes from the diagnostic turn row with the trial's `turn_id`, and only from its `stt_fallback` destination.
The row stays in `fallback_turns` in the trial output.
The aggregate delta must agree with that STT cause, or show no change when the turn has no STT fallback.
A missing or duplicate turn row, a TTS-only cause, or a different aggregate delta cannot prove the necessary STT fallback.
Missing diagnostics, invalid counts, and a counter decrease give an unsatisfactory result.

| Stage | Necessary delta |
| --- | --- |
| `before` | One fallback only: `health_timeout`, `health_error`, `server_unhealthy`, or `open_error` |
| `speech`, `pause` | One fallback only: `stream_closed`, `message_timeout`, or `stream_error` |
| `after-final` | No fallback |

The aggregate counts belong to the full config entry.
Keep other turns not active during each trial.
An unrelated fallback can cause an unsatisfactory result.

## Deadline and stop timing

The deadline is 20 s after the HA `run-start` event, not after the fault request.
The CLI has no deadline override.
The classifier checks the recorded deadline against the turn start.

WARNING: Calibration stops the isolated server five times for a run with `speech`.
Without `speech`, calibration stops it one time.
There are no more calibration stops for each recording or trial.
The runner measures each `docker stop --time 0` with a monotonic clock.
After each sample, it restarts the server and waits for `Running=True` and `Health.Status=healthy` before the next sample.
The measurement does not include server restoration time.

The output and each trial keep all samples in `stop_latency_samples_seconds`.

For `speech`, latency `L` is the median of the five samples.
Margin `M` is half the difference between the maximum and minimum samples, plus 20 ms.
The planned request is the voice-window midpoint minus `L`.
The request can be before the voice window, also when `L` is larger than that window.
A window shorter than `2 × M` is `unavailable` and starts no fault.
Equality is possible.

A request before audio starts is also `unavailable`.
The runner changes `L` and `M` to samples, rounded up, and records the two values in seconds.

For `pause`, the target stop time stays at the window start plus 25% of its length.
The planned pause request is earlier by the measured latency.
Audio frames limit request timing to 20 ms steps.
The runner records the actual stop sample and rejects a stop that ends at a sample not in the labelled window.
A changed stop latency can cause an unsatisfactory trial.

## Append-only records

Run `task records-check` to check `docs/verification.md` against the merge base of `main` and `HEAD`.
`task check` includes this check.
The check includes uncommitted file changes.
It names each changed, deleted, or new line before the end of the previous record.
Add new records only at the end of the file.

CI fetches the full history.
On a push, the check uses the previous `main` commit from the push event.
On a pull request, it uses the merge base of `origin/main` and `HEAD`.
An unreadable base commit causes failure.
To select a base commit, use `uv run --locked python tools/check_records.py --base "$BASE_COMMIT"`.
