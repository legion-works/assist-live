# Privacy

The project is experimental and has no release.

## Destination checks

The [public design](design.md) states: "All device control uses the HA agent and HA exposure rules."
Sensors and diagnostics only read integration state. They add no device-control path.

HA's `local_only` option checks configured server destinations, HA fallback destinations, and speaker events.
With this option on, HA uses server STT or TTS only when health reports `local_only=true` and `local` for that part.
If a check is unsatisfactory, HA uses the fallback. With local only on, HA does not use an older server without these fields.
Set `local_only` on the server to check server providers and speaker addresses.

Diagnostics show the last health values as `server_local_only` and `server_destinations`.
Missing values show as `not_reported`. Unknown server destinations stay `unknown`.
The health speaker destination does not select STT or TTS. The server checks speaker addresses before use.

These checks are not a network firewall.
They cannot examine the traffic of other HA integrations.
The local-only guarantee does not include the conversation agent.
Diagnostics show the selected agent's `iot_class` and destination. They do not stop the agent.

An unknown fallback must have an explicit local decision before use with `local_only`.
The options flow stores that decision and its UTC time.
Diagnostics show the audit record, without the entity name.

Per-turn fallback destination rows show `fallback_reason` when a server destination check refused use.
The named values are `server_not_local_only` and `server_destination_not_local`.
Other values show as `unknown`, without content disclosure.
A known cloud fallback cannot use that decision.
HA checks the current destination again before each fallback call and each mid-turn TTS continuation.

## Content and records

The [pinned protocol](https://github.com/legion-works/assist-live-server/blob/f6ac2d61da342dc962db446d9e9d5fab8f80ee95/docs/protocol.md) states:
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

Default diagnostics do not include tokens, server addresses, audio, transcript text, TTS text, or speaker names.
They include counts, destination categories, timing quality, and selected audio-format fields.
The per-turn service list includes each used fallback, not only the configured engines.
HA keeps one last-turn summary for each satellite in memory. It does not keep that summary after a restart.

TTS failure events can contain the full reply text.
The optional failure notification can also show that text.
These are not part of default diagnostics. Use content privacy rules for event listeners.
