# Privacy

The project is experimental and has no release.

## Destination checks

The [public design](design.md) states: "All device control uses the HA agent and HA exposure rules."
Sensors and diagnostics only read integration state. They add no device-control path.

HA's `local_only` option checks configured server destinations, HA fallback destinations, and speaker events.
With this option on, HA uses server STT or TTS only when health reports `local_only=true` and `local` for that part.
If a check is unsatisfactory, HA uses the fallback. With local only on, HA does not use an older server without these fields.
Set `local_only` on the server to check server providers and speaker addresses.

Diagnostics show current server health as `server_healthy` and the last satisfactory check time as `server_last_healthy` (UTC ISO time).
Before a satisfactory check, the time and `server_last_known` are `null`.
The group `server_last_known` contains the last satisfactory health values, not current server state.
Its fields are `local_only`, `destinations`, `tts_voice_count`, and `tts_default_voice`.

Missing local-only or destination fields show as `not_reported`. Unknown server destinations stay `unknown`.
The destination map includes only `stt`, `tts`, and `speaker`, with values `local`, `cloud`, or `unknown`.
The health speaker destination does not select STT or TTS. The server checks speaker addresses before use.

These checks are not a network firewall.
They cannot examine the traffic of other HA integrations.
The local-only guarantee does not include the conversation agent.
Diagnostics show the selected agent's `iot_class` and destination. They do not stop the agent.

An unknown fallback must have an explicit local decision before use with `local_only`.
The options flow stores that decision and its UTC time.
Diagnostics show the audit record, without the entity name.

Per-turn fallback destination rows show `fallback_reason` with the same value as the event's `reason`.
The destination-check values are `not_local_only` and `destination_not_local`.
Other values show as `unknown`, without content disclosure.
Diagnostics count each known value in `fallback_reasons`. See the [cause table](configuration.md#fallback-and-timing).

A known cloud fallback cannot use that decision.
HA checks the current destination again before each fallback call and each mid-turn TTS continuation.

## STT name hints

The integration sends entity names, aliases, and area names only to the configured assist-live server.
It sends them only when server health reports `stt_hints=true` for a local STT provider.
The server keeps these names in memory only. It does not send the list to a cloud provider.

The integration keeps its list in memory too. It does not add names to logs, diagnostics, repair issues, or timing events.
Diagnostics contain only counts, the list digest, the send result, and the time of the last satisfactory send for each satellite.
The failure counter and error-class log contain no names. See [configuration](configuration.md#stt-name-hints) for the send conditions.

## Content and records

The [pinned protocol](https://github.com/legion-works/assist-live-server/blob/d314aa4f128bfdc12ccb9e6e0ff3f7d8c1a10cd3/docs/protocol.md) states:
"This message does not change transcript bytes."
This rule applies to `speaker_result`.
HA sends the transcript to its pipeline when it gets `final`. It does not wait for speaker metadata.

A different read waits for speaker metadata after the `final` transcript, with one 10.6 s deadline.
This includes 5 s for server address resolution, 5 s for HTTP, and the 600 ms message deadline.
Heartbeats do not extend the total deadline.
`speaker_metadata_missing` counts reads that end at the deadline or stream close without a recorded result.
It checks the two streams at that time.
Turn closure does not cancel the read. Entity removal cancels it without a missing-result count.

HA keeps a maximum of one pending speaker read for each satellite and eight for each integration.
A new STT turn cancels the previous read for that satellite first.
At the integration limit, HA cancels the oldest pending read before it adds a new read.
These cancellations increase `speaker_superseded` only when the two streams have no recorded result at that time.
They do not increase the missing-result count or change the repair issue.
`speaker_not_identified` counts received local results without a speaker name.
Subsequent timing results apply the destination policy, without a change to earlier diagnostic counts.

These limits bound pending audio sockets only. The server applies its own limits to audio sockets and timing subscriptions.

An empty or missing speaker name with destination `local` means no identification, not a checked local destination.
HA adds no speaker destination row, fires no speaker event, and does not change the repair issue for this result.
Diagnostics increase only `speaker_not_identified`. This result is not missing.
The same rule applies when the server timing event has no name and destination `local`.

A cloud destination without a name means that a cloud plug-in checked the audio but gave no speaker name.
HA records that cloud destination and applies the local-only repair when applicable. It does not fire a speaker event without a name.
This cloud result does not increase `speaker_not_identified`.

The speaker event keeps the server's name, distance, and destination.
Names do not go into logs or default diagnostics.
A cloud speaker result with HA local-only checks on causes a repair issue.

The audio stream and timing-event stream use the same check, without duplicate events or policy counts for a recent result.
If the turn record expires first, only the record update stops. The policy check and speaker event continue.
That check cannot stop a completed server request.

The HA `record_transcripts` option and the turn request default to `false`.
The server also defaults to no transcript storage.
The two options must be `true` before the server can store a `final` transcript.
The `partial` transcripts do not go into the timing record.
HA keeps no runtime audio or transcript in its saved turn state.
Audio buffers are in memory only for the current request and its fallback.

The background timing sender keeps at most 512 pending events in memory, with a request in flight.
These events contain timing and service metadata, not audio, transcripts, or reply text.
After a send failure, pending events wait for a satisfactory maintenance health check and can outlive their local turn records.
Integration shutdown cancels the sender and discards pending events without a last send or disk write.
Diagnostics count queue overflow and refused-event removals in `timing_events_dropped`. This counter contains no event content.

Refused timing events do not put event data or server error messages in the logs.
The `turn_opens_dropped` counter contains no turn or event data.

Default diagnostics do not include tokens, server addresses, audio, transcript text, TTS text, or speaker names.
Each diagnostic turn row includes the latency sensor's `turn_id` attribute value, a correlation ID with no content.
They include counts, destination categories, timing quality, and selected audio-format fields.
The per-turn service list includes each used fallback, not only the configured engines.
HA keeps one last-turn summary for each satellite in memory. It does not keep that summary after a restart.

After STT fallback, `server_audio_samples` counts 16 kHz mono samples handed to the server client before failure.
`server_audio_last_chunk_samples` counts the last chunk in that hand-over.
The two values are nonnegative integers, not audio or text. They are 0 if no audio went to the client.
They contain no fallback replay samples and do not change during fallback.

Without STT fallback, the row does not have these fields.
The count does not prove server receipt. See the [write bound](configuration.md#fallback-and-timing).

TTS failure events can contain the full reply text.
The optional failure notification can also show that text.
These are not part of default diagnostics. Use content privacy rules for event listeners.
