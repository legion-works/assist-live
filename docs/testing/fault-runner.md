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

Each trial with `status: failed`, `status: unavailable`, or `status: invalid_injection` keeps `failure_messages` in the private results.
The list uses the string format `<Class>: <message>`. The `failures` list keeps its previous format.
Each message has a maximum of 500 characters. The class name and the ` [truncated]` marker are part of this limit.

Error text from HA responses has the same limit. Do not publish these private messages.
The runner uses `scrub_cleanup_error` to remove tokens, URLs, and addresses before it applies the 500-character limit.

This section gives the simulated-mode rules. The hardware section gives its own turn-specific cause rules.

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

The output keeps all measured stop latencies in `stop_latency_samples_seconds`.
This list includes the calibration stops and each test run with a measured stop, also an invalid test run.
Each test run keeps a copy of the samples that its plan used in the same field.
It keeps its own stop latency in `measured_stop_latency_seconds`.

For each subsequent plan, latency `L` is the median of all samples collected so far.
Margin `M` is half the difference between the maximum and minimum samples, plus 20 ms.
The same sample list supplies `L` and `M`.
For `speech`, the first plan uses the five calibration samples and the stop latency of each earlier test run.
For example, if `before` runs first, the first `speech` test run uses 15 samples.

The planned request is the voice-window midpoint minus `L`.
The request can be before the voice window, also when `L` is larger than that window.
A window shorter than `2 × M` is `unavailable` and starts no fault.
Equality is possible.

A request before audio starts is also `unavailable`.
The runner changes `L` and `M` to samples, rounded up, and records the two values in seconds.

For `pause`, the target stop time stays at the window start plus 25% of its length.
The planned pause request is earlier by the measured latency.
Audio frames limit request timing to 20 ms steps.
The runner records the actual stop sample.
Only a stop in the labelled window can supply a counted trial.
The window includes its start sample but not its end sample.

## Replacement test runs

Each execution of a trial slot is a test run, or `attempt` in the output.
For `speech` and `pause`, a stop not in the window gives `status: invalid_injection`.
The runner keeps the full test run in `attempts`, but does not count it as `passed` or `failed`.
It then makes a replacement test run for the same trial slot with the same recording.
Each test run keeps its fault point, `trial_slot`, and `attempt` number.

The maximum is three test runs for each slot.
If all three miss the window, the slot gives `unavailable` and the gate result is unsatisfactory.
A window that cannot supply a plan also gives `unavailable` without a stop.

Only the actual stop sample and the labelled window select a replacement.
The transcript, intent, operations, cause, and deadline do not select it.
A correct transcript cannot make an invalid stop satisfactory.
An incorrect transcript cannot cause a replacement after a stop in the window.
The `before` and `after-final` fault points keep one test run for each slot.

The `trials` list keeps one result for each completed slot, also an `unavailable` slot.
The `attempts` list keeps all test runs, also the counted test runs.
The shared summary in `faults.stages` gives these counts for each fault point:

- `counted_trials`: Trials with a stop in the window and an available baseline.
- `passed`: Counted trials that satisfy the acceptance rules.
- `failed`: Counted trials that do not satisfy the acceptance rules.
- `unavailable`: Slots without a counted trial.
- `invalid_attempts`: Test runs with a stop not in the window.

For the fault points without a window, the stop and baseline checks continue to select counted trials.
The overall rate and the report use only counted trials, not invalid test runs or unavailable slots.
Ten satisfactory counted trials for each of the four fault points are necessary for acceptance.

## Hardware trials

The [design](../design.md#phases-and-gates) gives the hardware fault criterion.
Get approval before playback or a server stop. Use only the isolated bench and approved test audio.
Each argument below is necessary. Do not use hardware mode with `--simulate` or `--native-matrix`.

```sh
uv run --locked python tools/fault_runner.py --hardware \
  --satellite "$SATELLITE" --playback-sink "$PLAYBACK_SINK" \
  --wake-audio "$WAKE_WAV" --ha-container "$HA_CONTAINER" \
  --turn-audio-archive "$PRIVATE_ARCHIVE" \
  --token-file "$ASSIST_LIVE_TEST_TOKEN_FILE" --pipeline "$TEST_PIPELINE" \
  --audio-root "$AUDIO_ROOT" --temporary-agent-pipeline \
  --output "$FAULT_RESULTS" --check
```

Supply the wake WAV in an external directory. It must contain mono PCM16 audio at 16 kHz.
The runner connects its PCM directly to the corpus PCM in one WAV, without a pause.
One `pw-play` process sends this stream to the explicit PipeWire sink.
The runner keeps each playback WAV. It does not remove audio.

Enable HA pipeline debug recordings in `/config/pipeline_recordings` before the run.
The container must have `python3`, and the runner host must have `docker cp`.
The archive must be external to the repository. Its directory mode is `700`, and each WAV has mode `600`.
The runner copies the turn WAV immediately after the HA run ends.

SHA-256 checks compare the source before and after the copy with the archived bytes.
The source stays in HA, and an unsatisfactory copy stays in the archive.
Private results contain recording paths, transcripts, and entity IDs. Do not publish them.

### Recording qualification

Before calibration or the first fault, the runner plays each candidate one time without a fault.
Qualification uses the same wake stream, `idle` wait, cool-down, turn identity checks, and temporary test-agent pipeline as a trial.
The shared baseline path sends the archived turn audio to the server engine and the fallback engine without a fault.
The two baselines must give the corpus intent and device operations before the recording is eligible.

Candidates start with the different recordings in the trial plan, in first-use order across fault points and slots.
If necessary, subsequent candidates come from the remaining corpus recordings in the same per-stage order.
Only held-out recordings are candidates. Pause slots use only pause recordings.

The target is the number of different recordings in the initial plan, not the number of trial slots.
Each fault point must also have its initial number of different recordings.
By default, `--qualification-limit` is two times this target. An explicit positive value changes this limit.
Qualification stops at sufficient eligibility, the candidate limit, or the end of the candidate list.

Trials select only eligible recordings in corpus order and use them again with the simulated-mode order rules.
A replacement test run uses the same recording. No more eligible recordings are necessary.
The runner does not qualify more recordings during the trials.
If the eligible count is not sufficient, the hardware gate is unsatisfactory with `reason: insufficient_eligible_recordings`.
No calibration or fault starts.

The private `qualification` section gives `candidates`, `eligible`, `target`, `required_by_stage`, `limit`, and `sufficient`.
Its `recordings` list keeps each qualification test run, the two baseline transcripts, `eligible`, and `cause`.
Causes include `no_wake`, `early_end`, `server_baseline_intent`, `fallback_baseline_intent`, `operations`, and `unavailable`.
Unavailable results keep private `failure_messages`, with the same message-length limit as trials.
The `trials` and `attempts` lists and trial denominators do not include qualification plays.

Add `--qualify-only` to the hardware command to measure eligibility without calibration or faults.
This flag also plays audio. Get bench approval for this flag.
With `--check`, its exit status checks qualification sufficiency only. It does not prove hardware fault acceptance.

### Satellite state and turn identity

The runner finds the satellite device through the entity registry.
The public `assist_pipeline/device/list` command supplies its pipeline select.
The same device must have one microphone mute switch with a recognized registry name or translation key.

The runner polls `get_states` at 0.25 s intervals until the satellite is `idle`, for a maximum of 30 s.
Mute must be `off` at each poll. If mute is not `off`, the runner gives an error immediately.
The runner does not operate mute.

If the satellite does not become `idle`, the test run gives `unavailable` with `unavailable_reason: satellite_not_idle`.
This cause does not select a replacement test run. The runner continues to the next slot with the usual rules.
The full 6.2 s wake cool-down starts after the satellite becomes `idle`, not while a previous reply continues.
After the cool-down, the runner checks mute and satellite state again before playback.
If the satellite is busy again, the runner starts the readiness wait and the full cool-down again.

The limit is three restarts: four cool-down cycles in total (the first cycle plus three).
If the satellite is busy after the fourth cycle, the test run gives `unavailable` with `unavailable_reason: satellite_not_idle`.

Each qualification, calibration, and trial test run records `idle_wait_seconds`, the total readiness-wait time on the monotonic clock.
It also records `cooldown_started_utc_ns`, the UTC start of its last cool-down, or `null` if no cool-down started.
The idle-wait total does not include cool-down time.

The runner records the initial pipeline select, selects the temporary test-agent pipeline, and checks its state.
A cancellation-safe `finally` puts back the initial selection and checks it before temporary-pipeline cleanup.
An unsatisfactory `restore` stops the run.

Before playback, the runner snapshots the debug run IDs, turn sensor, and WAV paths for the device and selected pipeline.
One new debug run, one new voice turn ID, and one new WAV are necessary.
The run's `run-start` must name this satellite.
The turn ID comes from the latency sensor, not a timestamp.
The archive record keeps the turn ID and `pipeline_run_id`.
More than one possible run, turn, or WAV gives `unavailable`.

In HA `2026.10.1` and `2026.10.4`, `assist_pipeline/websocket_api.py:273-307` gives the schema of `assist_pipeline/pipeline_debug/list`.
Its response contains `pipeline_runs` with `pipeline_run_id` and `timestamp`.
Lines `339-379` give `assist_pipeline/pipeline_debug/get`, with `pipeline_id` and `pipeline_run_id` as input and `events` as output.
The installed-HA contract test checks these response shapes and the satellite field.

These administrator-only commands belong to the test runner, not to the integration.
HA stores WAVs in the device and pipeline directories, not in a directory with the debug run ID.
Thus a unique result and recording correlation supply the connection. Time alone cannot supply it.

### Correlation and fault position

The runner uses first differences to decrease low-frequency room energy, then energy-normalized FFT cross-correlation.
It accepts positive and negative signal polarity. The minimum whole-reference overlap is 55%.
The absolute peak minimum is `0.18`. The second-peak ratio minimum is `1.15`.
The second peak must be more than 640 samples from the maximum peak.

Three high-energy windows, one from each third of the source, check the offset with the same thresholds.
Each window must agree with a maximum difference of 640 samples (40 ms) for a complete recording.
A minimum of two windows must agree before an offset can identify `early_end`.
The result records the peaks, ratios, window offsets, and largest offset difference.
No reliable correlation gives `unavailable`, unless the turn WAV is shorter than the complete source recording.

For a fault during `speech` or `pause`, diagnostics supply `server_audio_samples` and `server_audio_last_chunk_samples`.
The possible-send interval is `[server_audio_samples - server_audio_last_chunk_samples, server_audio_samples]`.
Subtract the correlation offset from the two edges to get recording samples.
The complete interval, expanded by the largest window-offset difference, must fit in the labelled window.
The window includes its first sample but not its end sample.

Missing counts give `unavailable`. Host clocks do not prove a fault position.

### Schedule and baselines

The stop-latency calibration follows satisfactory qualification and keeps its initial sample count.
For `speech` or `pause`, the runner first makes a maximum of three no-fault hardware tries to calibrate the playback interval.
The initial interval is the HA STT-start time plus the correlated offset, minus playback launch and the wake length.

Subsequent tries add an interval sample from the stop end, the possible-send interval midpoint, and the correlated offset.
The next schedule uses the median of all available interval samples, with invalid tries.
Each try keeps its input samples. These estimates schedule stops only.
The calibration tries are not trials, and the counted results do not include them.

Before each playback, the runner waits 6.2 s, because the satellite ignores a wake word for a short time after its last turn.
If the SHA-256 of an archived turn audio file does not agree with its record, the run stops, and the archive keeps all copies.

The `speech` schedule is playback launch plus wake length, window midpoint, and calibrated interval, minus measured stop latency.
The `pause` schedule keeps the quarter-window target.
The `before` fault stops the server before playback.
The `after-final` fault stops only after the public debug events contain `stt-end` for that run.

The recognized text, intent, operations, and deadline use the same acceptance checks as simulated mode.
The deadline stays at 20 s.

Hardware cause checks use only the trial's diagnostic turn row, not the aggregate `fallback_reasons_delta`.
The `stt_fallback` cause must agree with the fault type. An `after-final` trial must have no STT fallback.

TTS fallback is correct behavior after a hardware fault. The result reports its cause in `tts_failure_cause`.
The HA debug run must have one `run-end` and no `error` event.
A reply error makes the trial unsatisfactory.

Each hardware try with archived audio gets its own no-fault baselines through the shared HA pipeline sender.
The baseline input is that turn WAV, not the initial corpus WAV.
The diagnostics cause selects the engine, as in simulated mode.
The trial keeps the source hash independently. Its baseline hash names the archived turn audio.

No wake word, `early_end`, or a fault not in its window gives an invalid try.
The shared test-run loop replaces it with the same recording in the same slot, with a maximum of three tries.
These rules also apply to `before` and `after-final`.
The wake and complete-recording checks continue to apply.
Transcript or device-operation results do not select replacements.
Exhaustion gives `unavailable`, which makes the hardware gate unsatisfactory.

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
