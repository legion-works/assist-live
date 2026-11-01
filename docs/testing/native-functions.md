# Native voice functions and reports

Device replay and physical observations stay `NOT RUN` without observation records.
No baseline or native-device report is available at this time.
The [design](../design.md#phases-and-gates) gives the acceptance rules.

## Matrix

Run each row on the same satellite in the two server states.
Use `server_present` with the assist-live server available.
Use `server_absent` with the server unavailable and the configured HA fallback engines available.
Keep the same pipeline, voice, device, volume, and virtual targets.

| Function | Server present | Server absent | Observation | Observer |
|---|---|---|---|---|
| `announce` | Play the announcement without an STT turn. | Play the announcement without an STT turn. | HA service result and device playback events. | Automated |
| `start_conversation` | Play the prompt and open the microphone. | Play the prompt and open the microphone. | HA service result, playback, and microphone events. | Automated |
| `ask_question` | Play the question and give the selected answer. | Play the question and give the selected answer. | HA service response and follow-up turn events. | Automated |
| `timers` | Start, finish, and cancel a native HA timer. | Start, finish, and cancel a native HA timer. | Native timer callbacks and device alarm playback. | Automated |
| `stop_word` | Stop the spoken reply on the selected stop word. | Stop the spoken reply on the selected stop word. | Device playback end after the stop word. | Operator |
| `media_player` | Play and stop media through the native media player. | Play and stop media through the native media player. | Public media-player state and device playback. | Automated |
| `ducking` | Decrease media volume during voice and then set the initial volume. | Decrease media volume during voice and then set the initial volume. | Operator compares media volume before, during, and after voice. | Operator |
| `wake_selection` | Use the selected wake word, not the previous one. | Use the selected wake word, not the previous one. | Public selection state and device wake events. | Automated |
| `mute` | Stop microphone input while mute is on. | Stop microphone input while mute is on. | Operator checks the physical mute control and microphone response. | Operator |
| `leds` | Keep the native light patterns for each voice state. | Keep the native light patterns for each voice state. | Operator monitors the physical LEDs during wake, voice, reply, and mute. | Operator |
| `area` | Resolve an unnamed target in the satellite area. | Resolve an unnamed target in the satellite area. | Selected intent and actual virtual-target operation log. | Automated |
| `exposure` | Refuse a target that is not available to Assist. | Refuse a target that is not available to Assist. | HA intent error and completed empty operation log. | Automated |
| `agent_selection` | Use the conversation agent selected by the pipeline. | Use the conversation agent selected by the pipeline. | Public pipeline configuration and agent response. | Automated |
| `follow_up` | Open a new voice turn for a continued conversation. | Open a new voice turn for a continued conversation. | Microphone event and parent turn identifier. | Automated |
| `pipeline_selection` | Use the pipeline selected for the satellite. | Use the pipeline selected for the satellite. | Public selection state and selected engine records. | Automated |
| `debug_availability` | Keep the native HA pipeline debug view available. | Keep the native HA pipeline debug view available. | Operator opens the native HA pipeline debug view. | Operator |

A service response alone does not prove that the device played audio.
An operator must check physical LED and mute behavior.
Virtual observations cannot satisfy a device row.

## Record observations

Keep observations external to the repository until a publication data check is complete.
Each record identifies a `function` and `path` from the matrix.
Its `observation` contains these fields:

| Field | Content |
|---|---|
| `observer` | `automated` or `operator`, as the matrix specifies |
| `scope` | `device` |
| `passed` | Boolean result of the actual observation |
| `detail` | Actual trial behavior |
| `evidence` | Identifier of the event log, service response, or operator record |
| `observed_at` | Observation time with a time zone |

Set `ASSIST_LIVE_NATIVE_OBSERVATIONS` to the external JSON observation list.
A missing observation stays `null` and causes a `--check` failure.
A missing row, duplicate row, changed expectation, or unsatisfactory observation also causes a failure.
After the hardware trials, run:

```sh
uv run python tools/native_matrix.py --matrix docs/testing/native-functions.md --output "$RUNROOT/p0-native.json" --check
```

Without observations, the command writes an unobserved matrix and gives a failure status.
It does not make measurements or invent results.
Subsequent fault trials use the same runner and validator.

## Isolated virtual checks

These checks use only the isolated instance from [Virtual intent tests](virtual-intents.md).
They start and cancel native timers through the public conversation API.
An announcement service response without a satellite target is not proof of announcement playback.
The output includes virtual entity state before and after the calls and the timer cleanup result.

```sh
uv run python tools/native_matrix.py --matrix docs/testing/native-functions.md --virtual --token-file "$ASSIST_LIVE_TEST_TOKEN_FILE" --labels tests/data/labels.jsonl --output "$RUNROOT/native-virtual.json" --check
```

Virtual output has `scope: virtual` and cannot satisfy the device matrix.
Device playback, LED feedback, and physical mute stay unobserved without hardware.

## P0 report

First complete [device replay and calibration](device-replay.md) with the frozen held-out corpus.
Use the forwarding baseline with standard HA STT and non-streaming TTS engines.
Record engine versions, voice, endpoint settings, audio format, and hardware class with one immutable baseline configuration ID.
Keep unchanged native-pipeline rows isolated with `pipeline: native`.

Each replay trial makes one run row.
A trial that is not complete has `complete: false`, explicit missing values, and named `failures`.
It stays in the completeness denominator.
Missing transcript, intent, or operation test records cause an unsatisfactory gate result, not an exclusion.
The report refuses missing schema fields, smoke rows, unfrozen rows, tuning rows, and unresolved recording hashes before statistics.
After the device and matrix trials, run:

```sh
uv run python tools/report.py p0 --labels tests/data/labels.jsonl --split held_out --runs "$RUNROOT/p0-runs.jsonl" --calibration "$RUNROOT/p0-calibration.json" --native "$RUNROOT/p0-native.json" --output "$RUNROOT/p0-baseline.json" --markdown "$RUNROOT/p0-baseline.md" --check
```

Report schema version 1 includes counts, exclusions with causes, missing events, and unsatisfactory trials.
It divides replies into two groups at 60 characters.
Each recorded step has p50 and p90 values from linear interpolation at `(n-1)*p`.
Each rate includes its denominator and a two-sided 95% Clopper-Pearson interval.
The output names these methods.

An unavailable baseline end-decision event uses an explicitly named `stt_final` proxy.
Other missing internal events stay missing, with no invented timing value.
Acoustic values keep their estimated quality and measurement uncertainty.

The `mic_to_speech` step starts at the turn-audio start, not the microphone-open event.
Its start is `mic_open.t_utc_ns - data.pre_roll_ms * 1000000` in nanoseconds.
This includes voice in the audio history before the microphone-open event.
The step keeps the microphone event's quality and uncertainty.
For nonzero history, its method is `mic_open minus pre_roll_ms`.

A missing history field means 0. Invalid history causes refusal before statistics.

The `wake_to_mic` step ends at the actual microphone-open event.
Native matrix observations do not calculate audio-start times.
The integration latency sensor uses end-decision to playback-start time and stays unchanged.

## P1 report

Replay `safe` and `fast` against the same baseline configuration and calibration revision.
Pair baseline transcripts by recording ID and recording hash before word error rate computation.
The default `safe` setting must decrease p50 last-voice-to-playback latency by a minimum of 40% in each nonempty reply group.
The report shows `fast` independently. It does not replace `safe` with `fast`.

```sh
uv run python tools/report.py p1 --labels tests/data/labels.jsonl --split held_out --latency-profile safe --baseline "$RUNROOT/p0-baseline.json" --runs "$RUNROOT/p1-runs.jsonl" --calibration "$RUNROOT/p0-calibration.json" --faults "$RUNROOT/p1-faults.json" --native "$RUNROOT/p1-native.json" --output "$RUNROOT/p1-acceptance.json" --markdown "$RUNROOT/p1-acceptance.md" --check
```

Each pause row must have a Boolean `pause_cut` value from the trial.
A pause trial that is not complete without this observation causes an unsatisfactory pause gate result.
The pause gate uses 300 held-out commands, at most 3% cuts, and a maximum confidence limit of at most 6%.
A minimum of 30 executable-prefix commands must have no cuts.
Safe replay must have a minimum of 95% correct transcript and intent together.
Word error rate can be at most one percentage point more than the paired baseline rate.

### Rules to compare results

`tools/result_text.py:words` owns the text rule for correct commands, word error rate, and faults.
It changes letters to lowercase, removes punctuation, and changes English cardinal numbers to digits.
The range is 0 through 999999.
Hyphens divide number words: `twenty-five` equals `25`.
The rule accepts `and` after `hundred` or `thousand`, and `a hundred` equals `100`.
It keeps `and` between different numbers, such as `one and two`.

The rule accepts a cardinal number from 1 through 99 before `hundred`.
For example, `fifteen hundred and five` equals `1505`.
This rule also applies after `thousand`, if the result is in the stated range.
The rule adds these groups only when their total is at most 999999.
`nine nine nine` stays three numbers, not `999`.

Ordinal words stay words. The rule does not equate `first` and `1st`, or `twenty-first` and `21st`.
Other word differences stay errors: `a timer` does not equal `the timer`.
A different number also stays an error.

`tools/test_environment.py:intent_matches` owns the intent rule.
The intent name must agree, and each labelled slot must have the same value.
The rule does not compare `preferred_area_id` and `preferred_floor_id`.
An omitted part of a time (`hours`, `minutes`, or `seconds`) has the value 0.
More HA slots do not replace a missing labelled slot.

The two human sets must cause no incorrect operation.
The validator compares actual operations with the virtual intent inventory and the labelled slots.
An unavailable expectation or missing operation test records cause an unsatisfactory acceptance result.

The fault file contains `trials`, with ten entries for each `stage`: `before`, `speech`, `pause`, and `after-final`.
Each trial must have `turn_id`, `recognized_transcript`, `evaluated_intent`, `action_trace`, and `action_trace_complete: true`.
The fault gate compares the transcript with the labelled text and compares the operation log with `expected_operations`.
It does not use `correct_transcript`, `operations_correct`, or `empty_transcript` flags as proof.
The empty-safe alternative has a recorded `stt-no-text-recognized` error from the same pipeline run and a complete empty operation log.
It has a recorded turn ID, empty `operations`, no agent record, and an end time at or before the fallback deadline.

HA reports this error before `stt-end` and before the agent runs, so transcript and intent fields can be `null` for this condition only.
Other missing transcript, intent, or operation records cause an unsatisfactory result.

Each trial also records the fault point, server stop result, fallback deadline, end time, and failure cause.
A missing trial, record that is not complete, incorrect result, or native-matrix failure causes a `--check` failure.
CI and publication checks stay isolated from these measurement results.

## Simulated fault runs

`tools/fault_runner.py --simulate` sends SHA-256-checked corpus audio through the public HA WebSocket pipeline API.
It starts at STT and stops only the isolated test server container.
It starts that container after each trial and at exit, and checks for `running` and `healthy` states.
The four fault points are before the turn, during voice, during the labelled pause, and after the complete transcript.
Use `--each 10` for ten trials for each point, or `--each 2` for a short test.

The runner checks the selected audio and fault boundaries before it changes the bench.
Without labelled pause recordings, select `--stages before,speech,after-final` for a short test.
The output records `pause` as `NOT RUN` with the cause.
Do not make replacement pause recordings for this test.
All four fault points with ten satisfactory trials for each point are necessary for acceptance.

Simulated output cannot satisfy that acceptance check.

```sh
uv run --locked python tools/fault_runner.py --simulate --each 2 --token-file "$ASSIST_LIVE_TEST_TOKEN_FILE" --pipeline "$TEST_PIPELINE" --audio-root "$AUDIO_ROOT" --native-matrix docs/testing/native-functions.md --output "$RUNROOT/simulated-faults.json" --check
```

The selected pipeline must use the isolated test agent to supply a selected intent and a complete operation log.
With explicit bench approval, add `--temporary-agent-pipeline` to make a temporary copy with the test agent.
This option keeps the same STT and TTS engines and does not set the copy as a satellite's pipeline.
It records the pipeline list and select states before and after the test, deletes the copy at exit, and checks equality.
The temporary path uses the virtual timer receiver and a new turn ID from the STT device's public sensor.

If the pipeline list has `assist-live f11-smoke`, the runner refuses to start and prints its ID. It does not delete that pipeline.
Cleanup waits through cancellation. Each delete call has a different 30-second timeout, with a maximum of two calls.

Each delete call opens a new authenticated WebSocket connection, also when the control connection has closed.
The control connection has an aiohttp heartbeat and a continuous reader.
The reader answers HA heartbeat frames during trials.

`cleanup_attempt_errors` records each unsatisfactory call number, error class, and message.
The message removes authentication tokens, URLs, and addresses.
`cleanup_snapshot_error` records an unsatisfactory read-back with the same fields.
If deletion is unsatisfactory, `TEMPORARY_PIPELINE_DELETE_FAILED` identifies the pipeline for operator cleanup, and the command exits with an error.

Without that record, the trial is unsatisfactory unless the same-run empty-safe error gives the necessary proof.
The output uses the M14 fields with `scope: simulated` and `mode: smoke`.
Missing device events stay `null`. Simulated trials cannot satisfy the hardware fault gate.

Native device rows and virtual rows have different lists for the two server states.
Do not put simulated output in `docs/reports/`.
Only a non-simulated command with `--check` can write JSON in `docs/reports/`.
Other repository output paths are not permitted.
Two-satellite concurrency runs in the HA test harness, not on a second physical device.

Hardware replay, calibration, native-device observations, and P1 acceptance stay `NOT RUN` without observation records.
