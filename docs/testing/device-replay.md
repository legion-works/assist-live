# Device replay and playback calibration

The device trial, smoke run, and 50-reply calibration are `NOT RUN: hardware not available`.
No calibration report contains hardware results at this time.
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
The output contains timing values, sample indices, transcripts, intents, and operation records, not microphone audio.
Do not save or publish ambient audio without written consent that includes public test-audio use.
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
It plays the wake word, waits for one device wake event and microphone-open event, then plays the command.
Duplicate wakes, unmatched turns, and missing device events cause an unsatisfactory sample result.
Timing records come through the pinned client's public subscription, turn query, and export methods.

Only smoke mode accepts `--limit`.
An unfrozen smoke run prints `NOT FROZEN corpus` and sets `frozen: false`.
All smoke rows have `mode: smoke`, also with a frozen corpus.
Acceptance validation refuses them.

## Acceptance runs

A frozen corpus manifest that agrees with the corpus records and each recording hash is necessary for acceptance.
The current corpus is not frozen.
The tool refuses acceptance with `BLOCKED corpus not frozen` until corpus collection and voice-boundary checks are complete.
These commands use hardware:

```sh
uv run python tools/device_replay.py --phase p0 --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --split held_out --wake-word --external-mic --clock-record "$CLOCK_RECORD" --acceptance --output "$RUNROOT/p0-runs.jsonl"
uv run python tools/device_replay.py --phase p1 --profiles safe,fast --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --split held_out --wake-word --external-mic --clock-record "$CLOCK_RECORD" --acceptance --output "$RUNROOT/p1-runs.jsonl"
```

Without `--limit`, replay also uses acceptance mode when `--acceptance` is missing.
`--acceptance --limit 2` gives `LIMIT_NOT_ALLOWED_IN_ACCEPTANCE`.
`--profiles` is for P1 only.
The P1 `ProfileSelector` adapter is not available at this time.
It gives `NOT AVAILABLE: device profile selector` rather than pretend to change a setting.
Tests use a fake selector for the two settings.

Run schema version 2 includes the recording hash, partition, phase, `profile`, turn, run, and immutable baseline configuration identifier.
It includes `recognized_transcript`, `evaluated_intent`, `action_trace`, `action_trace_complete`, reply text, character count, and reply group.
Short replies have at most 60 characters.
Each P1 row connects to one P0 acceptance row with the same recording hash, partition, and configuration identifier.
The P1 row keeps that P0 transcript as `baseline_transcript`.

Rows keep the device and host times, event quality, event uncertainty, calibration revision, and external microphone `alignment`.
Device wake, microphone-open, playback-start, and playback-end records are mandatory.
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

The hardware trial, smoke run, and 50-reply calibration stay `NOT RUN: hardware not available`.
Do not make `docs/reports/p0-calibration.json` before those measurements are available.
