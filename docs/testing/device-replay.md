# Device replay and playback calibration

The device trial, smoke run, and 50-reply calibration have no result record in this repository.
The procedures below are not measurement results. Unobserved trials stay `NOT RUN`.
The [design](../design.md#phases-and-gates) gives the latency gates.
Synthetic tests check the code, not the device or its acoustic response.

## Hardware and consent

Use a Voice PE, a replay loudspeaker, and an external measurement microphone.
Connect the loudspeaker and microphone to a Linux computer with `aplay` and `arecord`.
Keep the speaker, device, and microphone positions constant for P0 and P1.
Keep the volume, sample format, HA engines, voice, and endpoint settings constant.
Identify that configuration with an immutable `ASSIST_LIVE_BASELINE_CONFIG_ID`.

Supply an explicit clock record with `--clock-record "$CLOCK_RECORD"` for each run.
For `same_host`, the server and replay process must use the same kernel UTC clock.
A container on the replay computer can satisfy this requirement.
The runner checks all resolved server addresses against loopback or local interface addresses.
It refuses a non-local address or an unavailable local-interface check.
The resulting clock offset and clock uncertainty are 0.

For a different computer, supply a `measured` clock record from an NTP or PTP monitor report.
Include `offset_ns` (server minus replay UTC), `uncertainty_ms`, and a nonempty `source` name.
Clock uncertainty must not be negative.
The runner records these values on each row and adds clock uncertainty to the acoustic uncertainty.
Calibration subtracts the clock offset from server event times, then compares them with acoustic times.
The runner does not invent a remote clock measurement.

The clock files contain one of these JSON objects:

```json
{"kind": "same_host"}
```

```json
{"kind": "measured", "offset_ns": 25000000, "uncertainty_ms": 2, "source": "ptp-tracking-report"}
```

WARNING: Get consent from each person in the room before microphone capture.
Use an isolated room without household voice.
The capture adapter keeps PCM in memory and clears its buffer after each sample.
Do not save or publish ambient audio without written consent that includes public test-audio use.

The output contains timing values, sample indices, transcripts, intents, and operation records, not microphone audio.
The output path must refer to a location external to the repository.
Replay and the device recorder replace each output file only after the new file is complete.
Only the file owner can read or write the file.
Check output for personal data before publication.

Use only the isolated [virtual intent environment](virtual-intents.md).
Enable transcript recording explicitly in the server policy and the integration turn settings.
Use the constant short and long replies from that environment.
Do not use household targets.

## Inputs

Set these environment keys external to the repository:

| Variable | Content |
|---|---|
| `ASSIST_LIVE_URL` | Server URL for the pinned client |
| `ASSIST_LIVE_TOKEN_FILE` | External JSON file with a `token` string |
| `ASSIST_LIVE_SATELLITE` | Anonymous key of the test device |
| `ASSIST_LIVE_WAKE_AUDIO` | WAV recording of the selected wake word |
| `ASSIST_LIVE_OUTPUT_DEVICE` | ALSA loudspeaker device, or `default` |
| `ASSIST_LIVE_CAPTURE_DEVICE` | ALSA external microphone device, or `default` |
| `ASSIST_LIVE_RUN_ID` | New identifier for this run |
| `ASSIST_LIVE_BASELINE_CONFIG_ID` | Immutable identifier of the baseline configuration |
| `ASSIST_LIVE_CALIBRATION_REVISION` | Identifier for this calibration series |
| `ASSIST_LIVE_OPERATION_RECORDS` | New external JSONL file from the public recorder |
| `ASSIST_LIVE_BASELINE_RUNS` | P0 acceptance JSONL for paired P1 rows |

Do not put a token in command arguments or output.
The recorder must start before replay and finish its file before replay ends.
The replay tool refuses a recorder file from before the run.
It reads that file one time, after all device samples.

Each recorder record must contain `turn_id`, `turn_type: voice`, `device_id`, `run_id`, and `baseline_configuration_id`.
The turn, run, and configuration identifiers must agree with the replay row.
It must also contain the public intent, operation, service-call, response, and text-delta fields from the virtual intent environment.
The validators check those fields against the corpus records.
The transcript must equal the device STT transcript, not a copied corpus value.

The recorder must supply `operations` and `completed: true`.
Each operation contains `service`, `target`, `arguments`, and `t_monotonic_ns`.
An empty operation log must have the explicit completed-recorder flag.
The bridge maps these fields to `action_trace` and `action_trace_complete` in run schema version 2.
`BRIDGE_FIELDS` and `BRIDGE_OPERATION_MAP` in `device_replay.py` give the bridge contract.

The [passive device recorder](virtual-intents.md#device-operation-bridge) connects public sensor turn IDs to conversation context IDs.
It supplies monotonic operation times and an explicit completed flag through public events.
The text-only recorder has no device turn connection and cannot satisfy device replay.

## Smoke run

This command uses all three hardware items:

```sh
uv run python tools/device_replay.py --phase p0 --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --split held_out --wake-word --external-mic --clock-record "$CLOCK_RECORD" --limit 2 --output "$RUNROOT/replay-smoke.jsonl"
```

The tool checks each recording's SHA-256 before it plays the wake word or command.
The [measurement report rules](measurement-rules.md) give the counter inputs and uncertainty rules.
It plays the wake word, waits for one device wake event and microphone-open event, then plays the command.
Duplicate wakes, unmatched turns, and missing device events cause an unsatisfactory sample result.
Timing records come through the pinned client's public subscription, turn query, and export methods.

### Turn audio history

The `mic_open` event can include `data.pre_roll_ms`, an integer from 0 to 1000.
A missing field means 0 for older firmware.
The runner refuses other values as invalid test records, without a limit adjustment.
It keeps the field in the event data and the exported microphone-open record.

Turn audio starts `pre_roll_ms` before the `mic_open` timestamp.
This audio start can be before the wake event.
The runner waits for the actual microphone-open event before command playback.
History is not proof that the device can receive a new command.
The external microphone uses the captured command to align audio, not the microphone-open timestamp.
The [report](native-functions.md#p0-report) uses the audio start for voice latency.

Only smoke mode accepts `--limit`.
An unfrozen smoke run prints `NOT FROZEN corpus` and sets `frozen: false`.
All smoke rows have `mode: smoke`, also with a frozen corpus.
Acceptance validation refuses them.

## Acceptance runs

A frozen corpus manifest that agrees with the corpus records and each recording hash is necessary for acceptance.
The current corpus is not frozen.
The tool refuses acceptance with `BLOCKED corpus not frozen` until corpus collection and voice-boundary checks are complete.
Volunteer pause recordings are necessary for P1 acceptance.

These commands use hardware.
Set `HA_URL`, `HA_TOKEN_FILE`, `INTEGRATION_ENTRY`, and `DEVICE_LOSS_ENTITY` for the same device and HA instance.
See [counter sources](measurement-rules.md#missing-events).

```sh
uv run python tools/device_replay.py --phase p0 --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --split held_out --wake-word --external-mic --clock-record "$CLOCK_RECORD" --ha-url "$HA_URL" --ha-token-file "$HA_TOKEN_FILE" --integration-entry "$INTEGRATION_ENTRY" --device-loss-entity "$DEVICE_LOSS_ENTITY" --acceptance --output "$RUNROOT/p0-runs.jsonl"
```

Without `--limit`, replay also uses acceptance mode when `--acceptance` is missing.
`--acceptance --limit 2` gives `LIMIT_NOT_ALLOWED_IN_ACCEPTANCE`.

### P1 runs by `profile`

The `safe` and `fast` settings use the same values.
The minimum time without voice is 400 ms, and the maximum is 1500 ms.
The Smart Turn limit is 0.8.
P1 runs compare the `profile` names, not different thresholds, until measurements give different values.

Use the position rule for P1 pause trials.
Enable HA debug WAV files as in the [fault runner](fault-runner.md#hardware-trials).
Set `HA_CONTAINER`, `HA_DEVICE_ID`, and `HA_PIPELINE_NAME` for the same isolated HA container, device, and pipeline as the replay.
Do not start other runs on that device and pipeline during a trial.

Start the isolated test server with `ASSIST_LIVE_TURN_PROFILE=safe`.
For each run, use a new `ASSIST_LIVE_RUN_ID` in the replay process and the recorder.
Use a new `ASSIST_LIVE_OPERATION_RECORDS` path and a new output path for each run.

```sh
uv run python tools/device_replay.py --phase p1 --profile safe --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --split held_out --wake-word --external-mic --clock-record "$CLOCK_RECORD" --ha-url "$HA_URL" --ha-token-file "$HA_TOKEN_FILE" --integration-entry "$INTEGRATION_ENTRY" --device-loss-entity "$DEVICE_LOSS_ENTITY" --ha-container "$HA_CONTAINER" --ha-device-id "$HA_DEVICE_ID" --ha-pipeline-name "$HA_PIPELINE_NAME" --acceptance --output "$RUNROOT/p1-safe.jsonl"
```

After the first run, restart the test server with `ASSIST_LIVE_TURN_PROFILE=fast`.
Do not change the server during a run.
Keep the baseline configuration identifier and calibration revision the same in the two runs.

```sh
uv run python tools/device_replay.py --phase p1 --profile fast --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --split held_out --wake-word --external-mic --clock-record "$CLOCK_RECORD" --ha-url "$HA_URL" --ha-token-file "$HA_TOKEN_FILE" --integration-entry "$INTEGRATION_ENTRY" --device-loss-entity "$DEVICE_LOSS_ENTITY" --ha-container "$HA_CONTAINER" --ha-device-id "$HA_DEVICE_ID" --ha-pipeline-name "$HA_PIPELINE_NAME" --acceptance --output "$RUNROOT/p1-fast.jsonl"
```

After the two runs, put the server back in its initial configuration.
Use a run file only if the checks before and after its trials were satisfactory.
If a check is not satisfactory, do that run again before you make the report.
Make a new private JSONL file for the [P1 report](native-functions.md#p1-report):

```sh
sh -c 'umask 077; set -C; cat "$1/p1-safe.jsonl" "$1/p1-fast.jsonl" > "$1/p1-runs.jsonl"' sh "$RUNROOT"
```

`--profile` is for P1 only. It does not change the server setting.
Replay reads `turn_profile` from the authenticated server `/health` before and after the trials.
It gives `MISSING_TURN_PROFILE` if the field is missing or invalid, or `TURN_PROFILE_MISMATCH` if it is different from `--profile`.
It gives `TURN_PROFILE_HEALTH_UNAVAILABLE` if it cannot read the response.

If the first check is not satisfactory, replay does not start the trials.
If the last check is not satisfactory, replay sets `complete: false` on all rows in the file and exits with an error.

Run schema version 2 includes the recording hash, partition, phase, `profile`, turn, run, and immutable baseline configuration identifier.
It includes `recognized_transcript`, `evaluated_intent`, `action_trace`, `action_trace_complete`, reply text, character count, and reply group.
Short replies have at most 60 characters.
Each P1 `replay` row connects to one P0 acceptance row with the same recording hash, partition, and configuration identifier.
The P1 row keeps that P0 transcript as `baseline_transcript`.

For `safe` and `fast`, the runner uses the held-out `replay` and `pause` rows.
A P0 transcript is not necessary for a pause row.

With the three debug recording options, the runner looks for one new STT debug WAV in the specified device and pipeline.
It copies the WAV to a private temporary directory in the output directory, checks its hash, and removes the copy after it reads it.
It does not remove the HA recording or include audio in the output.

A P1 command with none of the debug recording options is clock-only for pause trials.

The server event `end_decision` can give `end_sample`, the number of 16 kHz mono samples at the decision.
The runner fits the HA debug WAV to the external capture with the same correlation and window checks as the fault runner.
The WAV can start before the external capture, but its end must be in the capture and all three fit windows must agree.

The runner adds `speech_start` data `onset_ms` to the fitted WAV start.
This first voice estimate must agree with the start of voice on the capture.
The maximum difference is 50 ms plus the uncertainty of the WAV fit.

The runner adds `end_sample` to the fitted WAV start.
It uses the capture fit to find the last end in `pause_intervals_ms`.

It adds the uncertainty of the WAV fit and the capture fit to the pause end.
It rounds a fraction in that margin to the next integer sample.
It writes `pause_rule: position` and counts the observation as a cut only if the decision is before that limit.

If the sample position is missing or invalid, the runner uses the clock rule with `pause_rule_reason: no_end_sample`.
It also uses the clock rule with `no_debug_recording` if there is no unique new WAV, or `fit_failed` if it cannot fit or use that WAV.
It uses `fit_inconsistent` if the first voice times do not agree or if data for the check is missing.

For the clock rule, the runner calculates an interval from the first voice in the capture to the server `speech_start`.
This interval is not only the time to receive audio. It also includes the command-start check: 250 ms of voice and one 32 ms detector frame. If the voice starts in the first 400 ms of the turn, it also includes the time to the end of the start guard.

The runner subtracts the `alignment` uncertainty from the decision time and adds the interval to the last pause end.
If the adjusted decision time is before the adjusted pause end, it writes `pause_cut: true`. At or after that end, it writes `pause_cut: false`.

This rule is correct only if the time to receive audio at the end decision is not more than this interval.
The audio queue can become longer after the command start.
If the increase is more than the command-start check, the runner can write `pause_cut: false` for a turn that ended in the pause.

The runner writes `pause_rule: clock` only if it calculates `pause_cut`.
If it cannot compare the times, it writes `complete: false` without `pause_cut` or `pause_rule`.
It gives `MISSING_PAUSE_MAPPING` or `MISSING_PAUSE_DELAY_ESTIMATE`.
The P1 report gives one set of counts for `safe` and one for `fast`.

Rows keep the device and host times, event quality, event uncertainty, calibration revision, and external microphone `alignment`.
Device wake, microphone-open, playback-start, and playback-end records are mandatory.

A complete voice row also has one recorded `end_decision` and `stt_final` from the correct owners.
Use the TTS events for the applicable path.

For an STT or TTS handover, the report compares event times from the server and HA.
These two sources must use the same host clock, as on the test bench.
The [measurement rules](measurement-rules.md#event-owners) give the owners and failure codes.
The runner reads the transcript only from the `stt_final` event of the STT owner.
A server-only WebSocket sample cannot satisfy these checks.
A missing transcript, intent, or operation record also causes failure.

The runner writes one row for each sample trial, with failures.
Rows that are not complete use `complete: false` and named `failures`.
Unavailable observations have explicit missing values, not invented transcripts, intents, or empty operation logs.
Available events stay in the row when capture is not satisfactory.
The runner continues and gives a failure status if one trial is not complete.

Only setup failures before the first trial can stop the run without a sample row.
The [report](native-functions.md#p0-report) keeps unsatisfactory trials in its denominator.
Unavailable test records cause an unsatisfactory gate result.
Playback calibration accepts only complete samples.

## Acoustic boundaries

The aligner fits the command's 10 ms RMS envelope to the external microphone capture.
It searches the first two seconds for the command offset.
The last-voice marker is the labelled sample plus the fitted command offset.
The command-energy endpoint is only a cross-check.
Energy below 2% of the command peak is not an energy endpoint, also for a quiet tail above the noise floor.
A difference above 50 ms causes refusal, and uncertainty includes the permitted 50 ms difference.

Reply boundaries use the noise floor, not 2% of the reply peak.
The threshold is four times the lower-decile RMS noise, with a minimum of one PCM16 quantization step.
Reply regions must contain a minimum of five adjacent 10 ms frames above this threshold.
Short isolated sounds do not become the reply start or end when a sustained region follows.
Uncertainty also includes the distance to discarded sounds, because a short sound can be voice or a click.

No sound, ambiguous fits, boundary differences, missing replies, and truncated replies cause named failures.
The capture must have a minimum of 100 ms without sound after the reply.

The acoustic reply start is not the time when the playback command starts.
Each `alignment` has `quality: estimate` and `uncertainty_ms`.
The uncertainty includes frame size, command-fit variation, the cross-check limit, discarded sounds, capture buffering, and the clock record.
Capture sample 0 uses the time of the first PCM byte, not the process-start time.

`arecord` requests a 160-sample period and a 320-sample buffer at 16 kHz.
The capture uncertainty includes that 20 ms buffer, not the process-start interval.

This is an estimated acoustic boundary, not a measured sample callback.
Synthetic tones and noise check known boundaries, quiet tails, no sound, and clicks.
Acoustic boundary precision in a room stays unmeasured.

## Calibration

First collect a minimum of 50 different acceptance replies with external microphone `alignment`.
Set `ASSIST_LIVE_CALIBRATION_RUNS` to that external JSONL file, or supply `--runs`.
The run must use one baseline configuration and one calibration revision.
This command uses hardware measurements:

```sh
uv run pytest tests/test_device_replay.py tests/test_calibration.py -q && uv run python tools/calibrate_playback.py --device voice-pe --replies 50 --external-mic --output docs/reports/p0-calibration.json --check
```

For each playback boundary, the report gives p50 and p90 absolute error against the acoustic boundary.
Percentiles use linear interpolation at index `(n-1)*p` in sorted values.
The report also gives p50, p90, and maximum `alignment` uncertainty.
The tool accepts a proxy only when its p90 error plus p90 acoustic uncertainty is at most 100 ms.

A rejected proxy keeps latency gates blocked and makes `--check` give a failure.
The tool prints `PASS calibration n>=50` only after satisfactory count and proxy checks.
Fewer than 50 replies cause `CALIBRATION_TOO_FEW_REPLIES` and no count-success line.

The hardware trial, smoke run, and 50-reply calibration stay `NOT RUN` without observation records.
Do not make `docs/reports/p0-calibration.json` before those measurements are available.
