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
| `announce` | Play the announcement without an STT turn. | Same behavior through fallback TTS. | Service result and device playback events. | Automated |
| `start_conversation` | Play the prompt and open the microphone. | Same behavior through fallback engines. | Service result, playback, and microphone events. | Automated |
| `ask_question` | Play the question and give the selected answer. | Same behavior through fallback engines. | Service response and follow-up turn events. | Automated |
| `timers` | Start, finish, and cancel a native HA timer. | Same native timer behavior. | Timer callbacks and device alarm playback. | Automated |
| `stop_word` | Stop the spoken reply on the selected stop word. | Same stop-word behavior. | Playback end after the stop word. | Operator |
| `media_player` | Play and stop media through the native media player. | Same media-player behavior. | Public state and device playback. | Automated |
| `ducking` | Decrease media volume during voice and then set the initial volume. | Same volume behavior. | Compare volume before, during, and after voice. | Operator |
| `wake_selection` | Use the selected wake word, not the previous one. | Same wake-word behavior. | Selection state and device wake events. | Automated |
| `mute` | Stop microphone input while mute is on. | Same physical mute behavior. | Check the physical control and microphone response. | Operator |
| `leds` | Keep the native light patterns for each voice state. | Same physical LED behavior. | Monitor wake, voice, reply, and mute light patterns. | Operator |
| `area` | Resolve an unnamed target in the satellite area. | Same area rules. | Intent and actual virtual-target operation log. | Automated |
| `exposure` | Refuse a target that is not available to Assist. | Same exposure rules. | Intent error and completed empty operation log. | Automated |
| `agent_selection` | Use the conversation agent selected by the pipeline. | Same selected agent. | Pipeline configuration and agent response. | Automated |
| `follow_up` | Open a new voice turn for a continued conversation. | Same follow-up behavior. | Microphone event and parent turn identifier. | Automated |
| `pipeline_selection` | Use the pipeline selected for the satellite. | Same selected pipeline. | Selection state and selected engine records. | Automated |
| `debug_availability` | Keep the native HA pipeline debug view available. | Same debug view. | Open the native HA pipeline debug view. | Operator |

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

The two human sets must cause no incorrect operation.
The validator compares actual operations with the virtual intent inventory and the labelled slots.
An unavailable expectation or missing operation test records cause an unsatisfactory acceptance result.

The fault file contains `trials`, with ten entries for each `stage`: `before`, `speech`, `pause`, and `after-final`.
Each trial must have `turn_id`, `recognized_transcript`, `evaluated_intent`, `action_trace`, and `action_trace_complete: true`.
The fault runner records `correct_transcript` and `operations_correct` for a checked correct result.
The alternative must have `empty_transcript: true` and an explicit empty `operations` list.
A missing trial, record that is not complete, incorrect result, or native-matrix failure causes a `--check` failure.
CI and publication checks stay isolated from these measurement results.

Hardware replay, calibration, native-device observations, and P1 acceptance stay `NOT RUN` without observation records.
