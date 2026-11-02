# Measurement reports

The [measurement rules](../design.md#measurement-rules) give the report contract.
These tools do not supply hardware acceptance results.

## Integration receipts

The server owns its detector, transcript, and audio-production events.
The integration records `stt_final_received` when HA gets the transcript and `tts_audio_received` when HA gets the first audio byte.
These receipt events do not replace the server endpoints in a latency report.
On fallback, the integration owns `end_decision`, `stt_final`, and `tts_first_byte` for the fallback work.
The decision is a proxy with `decision_source: ha_audio_end`. Each `fallback_used` event gives a `reason` and `part`.

The integration keeps `conversation_id` empty and supplies a `turn_id` on each event.

After a server outage, the integration sends the turn open before its events, with the initial open time.
Refused events increase `timing_events_dropped` and do not stop the next event.
After a refused late open, the sender sends each event independently. The server can accept it if the turn is in storage.
The diagnostic counter `turn_opens_dropped` counts removed refused opens, not events.
Time limits and connection errors keep the pending event until a satisfactory maintenance health check.

## Sets

Each report row names its `set`: `replay` or `pause`.
The report gets this value from the corpus record for older input rows.
A different value on the input row gives `RUN_SET_MISMATCH`.
Latency and completeness use only replay rows.
Pause criteria use only pause rows.
A timing criterion with the two sets gives `MIXED_CRITERION_SETS`.

Wrong-action checks use the two sets, as specified in the design.

## Uncertainty

A step adds the uncertainty of its two endpoints.
A host event with measured quality and no uncertainty contributes 0 ms.
A device event must include the uncertainty of its conversion to host time.
An estimate without uncertainty makes the step uncertainty unknown.
The report does not ignore that unknown input in the percentile.
`uncertainty_unknown` gives the number of steps with an unknown input.

Playback calibration adds acoustic uncertainty and device clock uncertainty
before it compares the p90 error bound with 100 ms.
The acoustic uncertainty includes the replay-to-server clock uncertainty.
These two clock uncertainties are different inputs.

## Missing events

Replay rows keep one run window in `loss_counters`.
The window has start and end snapshots with `observed_utc_ns`.
Each snapshot has these counters:

| Field | Public source |
|---|---|
| `integration.value` | HA diagnostics: `timing_events_dropped` |
| `server.<satellite>.value` | Server `/health`: `sequence_gaps_by_satellite.<satellite>.missing` |
| `device.<satellite>.value` | HA state of the device `events_dropped` diagnostic sensor |

Supply `--ha-url`, `--ha-token-file`, `--integration-entry`, and
`--device-loss-entity` to the replay runner for the HA counters.
The HA token file uses the JSON `token` format of the fault runner.
Get approval before you supply a credential that the runner does not hold.
The runner uses its server token for `/health`.
It does not copy tokens into output.

The runner waits 1000 ms before the end snapshot for late device events.
For the server counter, `boot_id` identifies the device boot.
It is a non-negative integer, not a Boolean value.

With the same boot, the counter increase gives the count of missing events.
A decrease can come from late events. In that condition, use the remaining end count.
A different boot gives `reset_unknown`.

`counter_deltas` records end minus start with the flags.
It has one integration value and server and device values for each satellite.
A known unchanged counter gives 0. An unknown counter or changed boot or epoch gives null.
For a server counter, a negative value shows late events.
`server_missing_after_settle` records the remaining count in that condition.

An unknown boot at the start also gives `reset_unknown` with a known boot at the end.
With no boot at the start or end, `no_device_events` makes device completeness unsatisfactory.
It does not report missing events as an increase in the counter.

Integration diagnostics can supply `counters_since_utc_ns`.
The HA device state can supply that field or `boot_id` in its attributes.
The window records the method for each counter in `reset_methods`.
It does not use the state update time as the time when the counter went back to 0.

A counter that is not available has a null value and sets `loss_unavailable`.
A missing time when the counter went back to 0 or boot ID sets `reset_unknown`.
An increase that cannot connect to one turn sets `loss_present` for the run.
One of these flags makes each completeness criterion of the run unsatisfactory.
Missing snapshots also make completeness unsatisfactory.
Older producers without the necessary fields cannot satisfy this check.

The fault runner records integration snapshots through its diagnostic
request. Its server and device counters stay unavailable without those sources.
Its output keeps this limit apart from the fault outcome.

## Sentence timing

The [TTS sentence timing](../design.md#tts-sentence-timing) contract gives the fields.
The report groups sentence events by turn and TTS `request`.
With a missing `request`, the turn must have one server `tts_first_byte` and no other server first-byte event.
The report then uses `request_method: single_server_stream` and does not invent request 0.

A sole recorded request ID stays in the result. With no recorded ID, the request ID stays null.
With 0 or multiple server first-byte events, values stay unavailable with `MISSING_TTS_REQUEST_AMBIGUOUS_STREAM`.
Multiple recorded request IDs also give that result when a request ID is missing.

It measures `text_ms` and `first_byte_ms` from server `tts_text_received`.
Playback starts at the server `tts_first_byte` event.
For each sentence, the margin is the earlier audio time minus the wait for its first byte.
The report gives each margin, the smallest margin, the stop count, and the full stop time.

Earlier stops move playback forward. The full stop time does not count the same wait two times.
This is a server playback model, not a measurement of device stops.

The agent-text wait is the change in `text_ms` from the prior sentence.
For the first sentence, the prior value is 0.
Provider time is `first_byte_ms - text_ms`.
It includes the queue behind the prior sentence.
Missing events or anchors give unavailable values, not 0.
A stream with more sentences than its recorded events has unavailable full-stream values.

Dictionary fixtures check these values without a new client version.
The pinned client does not accept the new sentence events in report input.
The client change is necessary for end-to-end sentence input.

## Test-only pipeline observations

The fault runner owns its `assist_pipeline/run` request.
`pipeline_request_id` identifies its WebSocket command, not an HA run.
The runner keeps HA run and conversation IDs only when an event supplies them.
It connects its events to the turn through the turn correlation sensor.

`test_pipeline` keeps the first agent text delta, tool call and result deltas,
and intent end. Each pipeline event has `test_only: true`.
The fault runner ends at intent, so TTS start is `not_run`.
No P0 or P1 measurement criterion uses these observations.
