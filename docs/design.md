# assist-live design

## Status and scope

This is the public design for an experimental project. No release is available.
The integration supplies forwarding entities, fallback, turn records, and latency sensors.
The server supplies audio gateways, providers, and the turn detector. The ESPHome component sends authenticated timing events through UDP or WebSocket.
No P0 or P1 acceptance result is available in this repository.

The integration adds to the native Home Assistant (HA) Assist pipeline.
It does not replace the pipeline or supply a conversation agent in version 1.
Voice PE is the first hardware target.
The ESPHome component is generic, but version 1 tests only Voice PE.

## Native functions

Each satellite must keep these functions:

- The `assist_satellite` entity and `announce`, `start_conversation`, and `ask_question` services.
- Timers, the stop word, media playback, audio ducking, wake-word selection, mute, and LED feedback.
- Area context, exposed-entity rules, and the conversation agent that the pipeline selects.
- Follow-up turns, pipeline selection, and pipeline debug data.

All device control uses the HA agent and HA exposure rules.
The integration must not patch private HA classes.
The only non-public interface in the design is `tts.helper.get_engine_instance`.
A contract test examines that function on each selected HA version.
P4 proposes a public replacement.

HA developer documentation also gives two public entity-interface types that their component modules do not export through `__all__`:

- [`SpeechAudioProcessing`](https://developers.home-assistant.io/docs/core/entity/stt/#audio-processing) from `homeassistant.components.stt.models`.
- [`TTSAudioRequest`](https://developers.home-assistant.io/docs/core/entity/tts/#stream-audio) from `homeassistant.components.tts.entity`.

Contract tests pin their import paths and the fields that assist-live uses on each HA version in CI.

## Repositories and release boundary

| Repository | Content | License |
|---|---|---|
| `assist-live` | HA integration, HACS metadata, project documentation | Apache-2.0 |
| `assist-live-server` | Server, PyPI client, containers, HA app | Apache-2.0 |
| `assist-live-esphome` | ESPHome component and example configuration | GPL-3.0-or-later |

The HA app must not go into the HACS repository.
The project selects `2026.10.1` as the HA minimum.
The CI matrix tests HA `2026.10.1` and `2026.10.4`.
The baseline runtime is stock HA `2026.10.3`.
These versions are design targets, not a compatibility claim.

The domain is `assist_live`.
The project selects `integration_type: service` and `iot_class: local_push` for the manifest.
The HA minimum and those manifest values are project design decisions.
The manifest pins `assist-live-client==0.0.0`. That client has no PyPI release.

The release manifest must pin `assist-live-client==<exact version>`.
That version must be on PyPI before the integration release.
Local tests and CI install the client from the Git revision in `uv.lock`.

## Components and turn flow

The integration supplies one STT entity and one TTS entity for each satellite.
Each satellite selects a pipeline with its own entities.
HA does not give these entities a run identifier or satellite identifier.
The entity-to-satellite mapping supplies that context without private HA interfaces.
A repair issue reports two satellites that select the same assist-live pipeline.

HA 2026.10.3 exports `async_create_default_pipeline` and `async_update_pipeline` in `assist_pipeline`.
The integration uses them to make and update the pipeline of each satellite, with the TTS voice.
If HA refuses creation, use the manual instructions in [setup](setup.md#pipelines-and-voices).

The config flow selects the server, token, satellites, fallback entities, and the fallback voice.
The server configuration selects the providers and the turn values.
The TTS entity shows the voices that the server health response gives.

The server uses a versioned WebSocket protocol with JSON control messages and binary audio frames.
The first message selects the protocol version and functions.
Client and server contract tests must agree on that protocol.
The server uses no HA administrator credential. Its HA app does not request HA API access.

The Python client library uses the HTTP session that HA supplies.
If DNS prevents an HTTP request or WebSocket connection, the client makes one new system lookup without that session's DNS cache.
It retries one time through the same session to the resolved address.
The retry keeps the initial `Host` header and, for TLS, the server name and certificate-check name.

The lookup and retry share the initial deadline. They do not extend it.
Other connection errors do not cause this retry.
If recovery is unsatisfactory, the client gives its usual error, and the integration can use its fallback engine.

1. The satellite finds the wake word and sends a timing event.
2. The STT entity opens a turn and sends audio to the server.
3. The server runs STT and turn detection while the user speaks.
4. The server gives the complete transcript and the speaker result to HA.
5. HA gives the transcript to the selected conversation agent.
6. The TTS entity sends reply text as HA supplies it.
7. The server streams audio. The satellite reports playback start and end.
8. The native satellite opens the microphone for a follow-up question after playback.

The integration does not use administrator-only pipeline debug commands for turn records.
It records entity events and public satellite state.
Diagnostics show protocol version, provider destinations, and fallback counts.
A repair issue reports an unavailable server.
A sensor for each satellite shows `end_decision` to `playback_start` time and its quality.

### Background timing events

One background sender for each integration entry sends timing events in order for each turn.
STT results, TTS streams, and turn opens do not wait for timing events.
An outer timeout gives each event request a 2 s limit.
The integration sends a turn open before its events.

The turn open request is safe to send again.
The server compares a second request with the turn that has the same identifier: satellite, type, parent, conversation identifier, transcript permission, and open time.
If these values are the same, the server gives the response of a new turn and changes nothing.
If one value is different, the server refuses the request with `turn_conflict` (`409`).
After a refused late open, the server checks each event independently.

After a send failure, pending events wait for the next satisfactory maintenance health check.
The pending list holds at most 512 events, with the event in flight.
When the list is full, a new event removes the oldest event.
Diagnostics count these removals and refused-event removals in `timing_events_dropped`.
The counter `turn_opens_dropped` counts removed refused opens, not events.

Diagnostics give `counters_since_utc_ns`, the time when these counters started.
If this time changes, the integration started its counters again, and a report does not compare the two values.
Pending events can stay after their short-lived local turn records expire.

At integration shutdown, the integration cancels the sender and waits for cancellation.
It discards pending events without a last send or disk write.
Restart markers for saved open turns keep their startup behavior. They are not on a turn path.

### Satellite pre-roll

This firmware design awaits review. The setting is off by default, not on in a release.

The stock firmware starts the turn audio after `micro_wake_word` finds the wake word.
A word that the user speaks directly after the wake word is not in the turn audio.
The optional `pre_roll` setting of the ESPHome component keeps this audio:

- A virtual microphone reads the physical microphone and keeps a history of the most recent audio in RAM.
  The history has the left and right microphone signals, because HA selects one signal from the STT settings.
- Only `voice_assistant` uses the virtual microphone.
  `micro_wake_word`, the stop word, and the mute switches stay on the physical microphone.
- The wake handler arms history only while `voice_assistant` is in `IDLE`, immediately before the stock `voice_assistant.start`.
  Immediately after that start, `wake_started()` checks public `is_running()` and connects the history to that wake-word turn.
  An arm without this paired call cannot give history to a different turn.
- The component holds all historical and live frames from the arm until `on_listening`, the public STT-start signal.
  At that point, the voice assistant has made and cleared its new-turn buffers.
  No historical frame goes to the voice assistant before this gate.
  Replay sends history, then live audio, in order, without missing or duplicate samples unless the queue overflows.
  If the history and the audio from detection to the gate are together more than 1000 ms, the component removes the oldest history at the gate.
- The detection-to-gate limit is 1000 ms, not a 32 ms arm window.
  Above that limit, the component clears the queued audio and discards new frames until the gate or turn end.
  That turn gets no history.
- The history before the start of the turn audio has a maximum of 1000 ms.
  A device measurement sets the length, so that the history starts at about the end of the wake word.
  The measured default is 200 ms, from a device test with one speaker and beam search for the `final` transcript.
  With 450 ms, the end of the wake word became more words at the start of the transcript.
- After the STT-start gate, replay can start only when the HA connection accepts data.
  The replay rate is at most two times the real-time rate.
  The design specifies a minimum of 256 ms of free space in each voice-assistant microphone buffer, measured through a public ESPHome interface.
  This interface is a proposal for upstream ESPHome, not an available interface.
  Until then, the component uses time since replay start as a conservative estimate.
  It limits the estimated increase in backlog to 256 ms.
  With this estimate, replay is not at live audio before turn end. The time to turn end increases by the unsent history.
  The component counts and logs each audio frame that it removes.
- `on_stt_end` finishes the logical turn and clears its replay state.
  Run end, public `IDLE` state, and an actual microphone stop are fallbacks that can safely do this operation again.
  Between turns, current frames go directly to enabled sources immediately. Only bounded history stays in RAM.
- A turn from a button, a follow-up, or an announcement gets no history.
  A turn with the wake sound on gets no history, because the echo cancellation does not remove the wake sound.
- When the user mutes the microphone, the component clears the history.
  The history stays in RAM, and the component sends it only in a wake-word turn.
- The `mic_open` event data gives `pre_roll_ms`, an integer from 0 through 1000 ms.
  It counts only historical audio actually sent to the voice assistant in that turn, not dropped history.
  Each turn gets a new value, which is 0 for a turn without history.
  For a history turn, the event waits until history replay ends or the turn stops.
- The setting is off by default until the device test is satisfactory.

## End-of-turn and STT fallback

The STT entity keeps `requires_external_vad=False` constant on each path.
HA reads the value two times in a run, so server health must not change it.
The server uses Silero VAD, Smart Turn v3.2, and a check for words that cannot end a command.
The server ends a turn before the maximum time only with all three conditions:

- The time without voice is equal to or more than the minimum (initial default: 400 ms).
- The Smart Turn score is equal to or more than its limit (initial default: 0.8).
- The transcript does not end with words that cannot complete a command.

If those conditions do not apply, the turn ends at the maximum time without voice (initial default: 1.5 s).
The `safe` and `fast` settings keep all three conditions.
P1 selects their values with human recordings.
If `safe` does not satisfy the executable-prefix test, it uses only the maximum time without voice.

The turn audio can start with the end of the wake word, and the user can pause before the command.
Thus the turn detector uses a start guard:

- The command starts with the first voice run that is 250 ms or longer and that ends after 400 ms of turn audio.
  A voice run that ends in the first 400 ms is the end of the wake word, and the turn detector ignores it.
  The STT provider continues to decode all audio.
- Before the command starts, the three conditions above and the maximum time without voice do not end the turn.
  The time without voice starts at the command start.
- If no command starts in 5 s, the turn ends, and the server sends the transcript of all the audio.
  If voice continues at 5 s, the server waits until that voice becomes a command or stops.
- The guard applies to all turns, also to a follow-up turn.

P1 sets these three values with human recordings.

For the `final` transcript, the Parakeet provider uses the TDT beam search of the NeMo source (`default_beam_search`).
The beam width is 16, and the provider uses the NeMo score normalization.
It uses no language model, no word list, and no change to the audio.

For `partial` transcripts, the provider uses the greedy search, because it makes them frequently during a turn.
A device test showed that the greedy search can follow `blank` tokens through a full command and then give a short incorrect word.
The model score of the command was better than the score of that word.
The replacement rule below stays as a second guard.

For STT sessions with `final_redecodes=True`, the server can replace an empty or incorrect short `final` with the last `partial` sent in that turn.
Replacement occurs only with all these conditions:

- The `partial` has two or more words.
- The `final` has fewer words than the `partial`. An empty `final` has 0 words.
- No word occurs in the `final` and the `partial`.

The server uses Unicode `casefold`, removes punctuation separators, keeps internal apostrophes, and counts each word occurrence.
In all other conditions, the `final` stays unchanged, also when a shorter `final` keeps some words but removes `not`.
The server counts each replacement in `assist_live_stt_final_replaced_total` and adds `replaced: true` to the `stt_final` timing event.
The mark and count store no transcript text. Transcript storage uses the explicit option only.

The integration checks server health with a 200 ms limit at the start of each run.
If unavailable, it owns turn termination with `pymicro-vad` and 0.7 s without voice.
The pipeline VAD setting does not control this fallback path.

The maximum turn length is 15 s on all paths.
The integration buffers the complete current turn in memory (480 KB at 16 kHz, 16 bits).

The server sends a heartbeat each 100 ms, also when the user does not speak.
A closed connection or 600 ms without a message starts fallback during a turn.
The entity sends buffered audio, then live audio, to the fallback STT entity in order.

It does not start fallback after it gets a complete transcript.
Fallback has a 20 s limit from turn start. A second failure gives an empty result and records `stt_failed`.
It does not retry or start device control.

The STT request checks the fallback language, format, codec, sample rate, bit depth, and `channel` count.
An assist-live entity cannot be its own fallback.

### STT name hints

The integration makes a list of the names that Assist can use.
The list has the names and aliases of the entities that HA makes available to Assist, and the area names.
Each name has 1 to 64 characters and a minimum of one letter.
The integration changes names to lower case, removes spaces at the two ends, and removes duplicates.
A list has a maximum of 500 names in a stable order. Diagnostics record the number of names that the integration did not send.

The integration sends the list for each satellite with `PUT /v1/satellites/{satellite}/hints`.
It sends the list at setup and 5 s after a change in the entity registry, the area registry, or the Assist entity list.
It also sends the list when the health response gives a different list digest for the satellite, for example after a server restart.
The server refuses a list that does not obey these rules with `invalid_hints`.

The server keeps each list in memory only. The integration and the server do not write the names to logs or to the turn store.
Diagnostics and the health response give only the number of names and the SHA-256 digest of the list.
The health response gives `stt_hints`, which shows if the STT provider uses the list.

The local Parakeet provider uses the list in the beam search of the last transcript (context biasing).
Each token that continues a name gets a bonus of 1.0 by default.
If a hypothesis stops a name before the end of the name, the server removes the bonus that the tokens of that name gave.

The beam keeps 16 hypotheses. 8 positions go to the best hypotheses by the score with the bonus.
The other 8 positions go to the best hypotheses by the score without the bonus that are not in the first 8.
Thus the names cannot remove all the hypotheses that have no name. With an empty list, the search is the same as without names.
The transcripts during the turn do not use the list.
Cloud STT providers do not get the list.

Acceptance with 500 names: the 250 replay recordings get no name that the speaker did not say, and no more word errors than without names.
On the recorded device turns, the errors in names do not increase.

## TTS and fallback

The TTS entity sends each text part immediately and keeps a copy.
It does not add a full-reply buffer before the server.
If the server stops before audio starts, fallback gets buffered parts followed by new parts, one time each.
A streaming fallback streams audio. A non-streaming fallback gets the full text and shows a slower path in diagnostics.
The config flow refuses an assist-live fallback to prevent loops.

After audio starts, fallback can continue at the next sentence only with the same audio format.
If the formats do not agree, the entity ends audio, records `tts_failed`, and fires `assist_live_tts_failed` with the full reply text.
An optional `notify` service receives the full reply.
The integration does not run the conversation or device control again.

### Spoken replies

The integration has an `llm` platform.
It gives a prompt part to the HA Assist API (`assist`) and no tools.
It gives the prompt part only when the request comes from a satellite of the config entry.
The prompt part tells the agent these rules:

- The satellite speaks the reply aloud.
- The reply has plain spoken sentences only. It has no Markdown marks, lists, headings, tables, web addresses, code, or emoji.
- If the agent must get an answer from the user, the reply ends with one question.

HA opens the microphone again only after a reply that ends with a question mark.
The prompt part applies only when the conversation agent uses the Assist API.
An agent that does not use the Assist API does not get it.
The server text rules below apply to all agents.

### Spoken text

Before the server sends text to a TTS provider, it removes the Markdown marks that a person does not say.
These rules apply to all TTS providers:

- Remove these marks at the start of a line: `#` (heading), `>` (quotation), and the list marks `-`, `*`, `+`, `1.`, and `1)`.
- Remove the lines that contain only `---`, `***`, or `___`. Remove the lines that start a code area or end it (`` ``` ``).
- Remove emphasis marks (`*`, `**`, `_`, `__`, `~~`) and inline code marks (`` ` ``) only when they touch a word on one side and not on the other side. Thus `2 * 3` and `snake_case` do not change.
- Change the Markdown text `[text](address)` to `text`, and `![text](address)` to `text`. Remove HTML tags. Remove table rows that contain only `|`, `-`, `:`, and spaces. Change a `|` between table cells to a comma.
- The end of a heading line or a list line is the end of a sentence, also when the line has no end punctuation.

Sentence markers, `text_end`, and the sentence timing events use the character positions of the request text, not of the changed text.
Thus the HA fallback continues at the correct position.

The rules work on streamed text.
The server holds back a maximum of 2048 characters to find the end of `[text](address)`.
If it cannot find the end, it sends the held characters without a change.
The time is linear in the length of the text.
If the changed text of a sentence has no letters or digits, the server does not send that sentence to the provider.
The next sentence marker includes its characters.

## Provider and license selection

Parakeet TDT 0.6B v2 through `onnx-asr` stays the local STT default until a streaming model satisfies replay tests.
It decodes the full buffer at intervals. CC-BY-4.0 attribution is necessary.
Larger sherpa-onnx Zipformer and NVIDIA streaming FastConformer models are candidates, not approved defaults.
Deepgram Nova-3 live is the cloud STT candidate.

Each request must set `mip_opt_out=true`.

With `mip_opt_out=true`, Deepgram keeps audio and transcripts only for request processing, not after the response.
Request metadata stays retrievable for 90 days. Summarized usage stays longer.
See the [retention source record](https://github.com/legion-works/assist-live-server/blob/a888c3b5cc34e14f3ee9efb0bf76156df908de35/docs/providers/cloud.md#deepgram-retention-and-data-use).

OpenAI `gpt-live-transcribe` is a second cloud STT candidate.
The server opens one realtime transcription session for each turn and turns off the provider turn detection.
The server turn detector stays the only source of the end of the turn. At the end decision, the server sends the commit.

The last transcript is the `completed` event of the committed item. A `failed` or `error` event starts the HA fallback.
The adapter uses the limits of the Deepgram adapter.
The provider gives no word times, so the rule that replaces the last transcript does not apply.

By default, OpenAI keeps abuse-monitoring logs for a maximum of 30 days, and these logs can hold content.
A live check on 2026-11-02 found that the transcription session refuses 16 kHz audio.
The server changes the turn audio from 16 kHz to 24 kHz before it sends the audio.
In phase P1, a test compares Parakeet, Deepgram, and OpenAI and selects the cloud default.
Version 1 does not use voice-to-voice models such as `gpt-realtime-2.1`.
These models replace the STT, the HA conversation agent, and the TTS, and they stop the local fallback.

ElevenLabs `eleven_flash_v2_5` is the planned cloud TTS default.
Its WebSocket path uses a minimum 50-character chunk schedule and an end-of-reply flush.
The default 120-character schedule adds buffering after HA's 60-character threshold.

Piper 1.8.0 with the `en_US-lessac-medium` voice is the planned local TTS default.
A measurement on the development host selected it from five engine configurations.
The `assist-live-server` record `docs/research/q7t-local-tts.md` gives the results.
Full-file engines are fallback only.

A license does not stop the selection of a model, voice, runtime, or library.
Each component has a record with its pinned source, SHA-256, license, and attribution.
The integration and server code have the Apache-2.0 license.
The ESPHome component code has the GPL-3.0-or-later license.
Third-party components keep their own licenses, and `THIRD_PARTY_NOTICES.md` lists them.
The Silero VAD repository owner states that the v6.2.3 model file has the MIT license.

## Timing and turn identity

Device events contain protocol version, device ID, random 64-bit boot ID, sequence number, monotonic milliseconds, `stage`, and event data.
An HMAC-SHA256 tag includes each field in canonical byte order, also the `stage`.
The device token is the key and comes from ESPHome secrets.
The server rejects invalid tags and duplicate boot-ID/sequence pairs.
A new boot ID starts a new sequence and clock window.

P0 tests changed, reordered, duplicate, and post-restart events.
P0 selects UDP or WebSocket transport from measurements.
An unavailable server must not slow native satellite functions.

The record uses UUIDv7 `turn_id`, optional `parent_turn_id`, `turn_type`, available `conversation_id`, satellite, source, `stage`, and UTC nanoseconds.
Device events also keep device milliseconds and sequence number.
Each time has quality `measured`, `proxy`, or `estimate`.
Estimates include `uncertainty_ms`.

Media-player playback events stay proxies until calibration.

The clock offset is the smallest arrival-minus-device offset in the last 60 s or 50 events.
Uncertainty is the median offset minus that minimum. The server records missing sequence numbers.

The main interval is `end_decision` to `playback_start`.
The second interval is `speech_start` to `playback_start`.
The agent interval is `stt_final` to `tts_first_text`.
Reports give the quality of playback markers.

- An STT stream opens a `voice` turn for its satellite.
- TTS with no eligible voice turn opens a `tts_only` turn.
- A cancelled voice turn without `stt_final` closes as `turn_superseded` before a TTS-only turn.
- A new STT stream supersedes an open turn. One satellite has at most one open turn.
  If a turn becomes superseded while its server-open request waits, it cannot start a fallback.
  STT gives an empty result. TTS stops with `HomeAssistantError` and no playback.
  The integration writes a debug line without turn data.
- A voice turn uses the previous turn as its parent only if the server opened that turn and it closed with `playback_end`.
  The new turn must open less than 10 s after that event. At 10 s, it has no parent.
  The integration measures this interval from monotonic receipt time, not device time.
  The time to receive the event can change the interval relative to device time.
  An answer to `ask_question` uses its `tts_only` parent with the same rule.
- A superseded turn, a turn closed by the 120 s limit, and a turn from before HA restart cannot be parents.
  An administrative close keeps the time of the turn's last event, not the open time of the next turn.
- A turn closes at `playback_end` or after 120 s, not merely when the satellite reports `idle`.
- Device events wait for a turn of the same satellite for 5 s, then become `unmatched`.
- HA restart does not keep open turns. Their records stay not completed.

The record includes wake, microphone, voice, turn decisions, transcripts, speaker results, TTS, playback, state, fallback, and failure events.
It also includes superseded, unmatched, barge-in, and all late-result states.
Reports count unmatched events and show the TTS-only turns that P0 completeness measurements do not include.
The server stores timing data in SQLite for 30 days by default and supplies metrics and JSON Lines export.
It does not store audio. Transcript storage uses an explicit option.

### Measurement rules

Each `stage` value has one owner:

| `stage` values | Owner when the server does the work | Owner when HA uses its fallback |
|---|---|---|
| `wake_detected`, `mic_open`, `playback_start`, `playback_end`, `barge_in` | device | device |
| `speech_start`, `end_candidate`, `end_decision`, `stt_partial`, `stt_final` | server | integration |
| `tts_first_text` | integration | integration |
| `tts_first_byte`, `tts_sentence`, `tts_last_byte` | server | integration (`tts_first_byte` only) |
| `fallback_used`, `turn_superseded` | integration | integration |
| `satellite_state` | integration, with the source `ha_state` | integration, with the source `ha_state` |
| `stt_failed`, `tts_failed` | server | integration |
| `speaker_result`, `tts_text_received`, `unmatched` | server | server |
| `stt_final_received`, `tts_audio_received`, all `late_result_*` values | integration | integration |

- The integration records `stt_final_received` and `tts_audio_received` when it gets a server result. The server records `tts_text_received` when it gets the first text.
- The server refuses an event from a part that is not the owner of its `stage` value. The response gives the named error `wrong_owner`. The sender removes a refused event, counts it, and sends the next event.
- A turn has a maximum of 1024 stored events and 1 MiB of stored event data. The server refuses an event after these limits with the named error `turn_event_limit` or `turn_size_limit`.
- The report uses only the event of the owner. Two events of one `stage` value from the owner give the named error `duplicate_stage`. The recognized transcript comes only from the `stt_final` event of the owner.
- Each turn has a path: `server`, `fallback_stt`, `fallback_tts`, `fallback_both`, or `baseline`. A complete turn has each `stage` value that is necessary for its path, from the owner of each value.
- If the server is not available, the integration keeps the turn open request and its events. It sends them when the server is available again. The server keeps the initial open time and marks the turn `late_open`.

- The store keeps codes and numbers only. These are the fallback `reason` and `part`, the `end_decision` rule and source, the satellite state, `onset_ms`, and the TTS `request` number.
- The start of the voice is the start of the turn audio (`mic_open` minus `pre_roll_ms`) plus `onset_ms`. The uncertainty of a step is the sum of the uncertainties of its two events. An estimate with no uncertainty gives an unknown step uncertainty, not 0.
- Latency criteria use only the replay set, and pause criteria use only the pause set.
- Each run records the counters of removed, missing, and unsent events at its start and end. If the report cannot attach a missing event to one turn, no completeness criterion of that run is satisfactory.

Public HA interfaces do not give some values to the STT and TTS entities. These values are the conversation and pipeline run identifiers, the satellite, the first agent text, and tool times. The record keeps them empty in usual turns, and the report names the agent interval "agent and HA wait". A reply from the HA TTS cache does not go to the TTS entity, so the report shows its TTS source as `not_observed`. Test tools that start the pipeline run record these values as test results only.

### TTS sentence timing

- The server records one `tts_sentence` event for each sentence, at the time it sends the last audio byte of the sentence. The data has numbers only: `index`, `text_end`, `text_ms`, `first_byte_ms`, and `audio_ms`. The server measures `text_ms` and `first_byte_ms` from `tts_text_received` with its own clock.
- At the end of the stream, `tts_last_byte` gives `sentences` (the number of sentences) and `audio_ms` (the full audio time). A turn has a maximum of 256 `tts_sentence` events.
- For each sentence, the report gives the wait for the agent text (`text_ms` minus the `text_ms` of the sentence before it) and the provider time (`first_byte_ms` minus `text_ms`).
- The report calculates the playback margin of each sentence from `tts_first_byte`. The margin is the audio time of all earlier sentences minus the time until the first audio byte of this sentence. A negative margin shows a stop in the reply.

## Late results (P2)

`assist_live.deliver_response` accepts `job_id`, satellite, message, mode (`announce` or `ask`), optional `notify` fallback, and expiry.
It rejects a duplicate `job_id`.
It calls only `assist_satellite.announce`, `assist_satellite.ask_question`, and the configured `notify` service.
It writes each state to storage before the next step:

1. A new request is `queued`.
2. It waits for `idle`, no open STT stream, and no pending wake event, then stores `delivering` before the satellite call.
3. It becomes `spoken` only after measured playback end or an accepted calibrated proxy.
4. No such event in 60 s after the call means `uncertain`.
5. The stop word gives `cancelled`, with no notification.
6. Call failure or expiry before playback starts a configured `notify` call. With no `notify` service, the state is `expired`.
7. A satisfactory `notify` call gives `notify_sent`. An unsatisfactory call gives `notify_failed`.

HA restart in `delivering` must not start playback again.
It tries a notification that states possible prior playback and records uncertainty.
Restart after a `notify` call but before persistence can send the notification again.
The satellite speaks each result at most one time.

Results not marked `spoken` or `cancelled` get a configured `notify` call.
`notify_sent` shows service acceptance, not that a person read it.
The integration fires `assist_live_delivered` for spoken or notify-sent results and `assist_live_failed` for other terminal states.

Native announce services cancel an active pipeline.
The `idle`/wake checks decrease, but do not remove, the wake-to-announce race.
An ESPHome end signal can come before actual playback.
The end of the service call and `idle` do not prove playback.

## Local and cloud limits

Each provider, fallback, and speaker-ID plug-in declares `local`, `cloud`, or `unknown`.
Fallback destination selection uses the owning integration's IoT class.
Local-only mode refuses cloud and unknown destinations that assist-live controls.
An operator can mark an unknown fallback as local. Diagnostics keep that decision.

The guarantee does not include the selected conversation agent. Diagnostics show its IoT class.
Local-only mode is not a network firewall.

The first speaker-ID plug-in uses the Murdock HTTP API.
In local-only mode, the server checks for local addresses at configuration and at each connection.
The speaker result goes only to the turn record and `assist_live_speaker`. It does not go into the agent transcript.
Cloud audio uses explicit selection of cloud STT, fallback STT, or speaker-ID service.

## Phases and gates

All gates below are future requirements, not current results.
The replay set has a minimum of 200 labelled commands from a minimum of 10 human speakers, plus synthetic tests.
The pause set has a minimum of 100 commands with 400 to 800 ms pauses from a minimum of 10 human speakers.
A minimum of 30 pause commands must have a complete executable prefix that subsequent words change.
Recordings use CC0 or CC-BY-4.0, and volunteers give written consent.
Each recording has the last voice sample, transcript, and intent as test data.

Reports show replies of 60 characters or less in a different group from longer replies.
Each rate has its denominator and 95% confidence interval.


| Phase | Exit gate |
|---|---|
| P0: measurement | A minimum of 95% of replay voice turns have each event from wake to playback end. Calibrate playback proxies against an external microphone on a minimum of 50 replies. Report p50 and p90 error and baseline time for each step. Accept a proxy only when its p90 error plus the p90 of its measurement uncertainty is at most 100 ms. Replay includes device wake and timing events, not only a pipeline WebSocket call. |
| P1: fast turns | On the same hardware, each reply group has a minimum of 40% less p50 last-voice-to-playback time than P0. Pause cuts are at most 3%. The high end of the 95% confidence interval is at most 6%. Correct transcript and intent are a minimum of 95%. Word error rate increases at most one point from P0. The safe setting has 0 executable-prefix cuts and causes 0 incorrect device operations. |
| P1: faults | Test 10 server stops before a turn, during voice, during a pause, and after the complete transcript. Each gives the correct transcript or an empty result with no device operation. |
| P2: conversation | Test 10 faults of each type. Types: restart in `queued`, restart in `delivering`, and restart after `notify`. Also test wake 0 to 500 ms before announce, busy satellite, server restart, `notify` failure, and stop word. Each test gives the specified states and no second announcement. The `notify` call starts in less than 5 s after expiry. Report commands stopped by announcements. |
| P3: barge-in | Complete the firmware design before the hardware test. Find a minimum of 9 of 10 interruptions in less than 500 ms. Keep the first command word and conversation ID. Cause 0 incorrect device operations and at most one incorrect interruption in 10 minutes. Keep wake, stop, timers, announce, and mute functional. If results are unsatisfactory, keep barge-in off and document the stop-word method. |
| P4: upstream | Use `partial` transcripts only after HA releases the interface. Propose public run context, idle-only announce, public TTS lookup, and barge-in interfaces. |

P1 correct-command results, word error rate, and fault results use one text rule.
The rule changes letters to lowercase and removes punctuation.
It changes English cardinal numbers from words to digits, from 0 through 999999.
For example, `eight hundred ninety four` becomes `894`, and `twenty-five` becomes `25`.
It accepts `a hundred` and `and` in a number.
It also accepts `nineteen hundred` as `1900` and `twenty-two hundred` as `2200`.

Ordinal words stay words: `first` does not equal `1st`.
A different word, such as `the` for `a`, stays an error.

The intent name and each labelled slot value must agree.
The rule does not compare `preferred_area_id` or `preferred_floor_id` from HA context.
A missing part of a time (`hours`, `minutes`, or `seconds`) has the value 0.
The wrong-operation gate compares operations without a change to their values.

The P3 firmware design must specify the echo-cancelled microphone path and the use of interruption audio and unheard reply text.

## Open checks and quality limits

P0 must select device transport and calibrate playback markers.
It must check HA Marketplace rules on a released HA version before an acceptance claim.
Model cost on target hardware stays open.
P1 must compare streaming STT candidates with Parakeet on human recordings.

Integration CI runs lint, type checks, HA tests, hassfest, HACS validation, and secret checks.
ESPHome must have a compile check in its repository.
A compiled firmware release must have a GPL source-obligation review.

See [upstream contracts and prototype limits](research/contracts.md).
