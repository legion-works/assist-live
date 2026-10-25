# assist-live design

## Status and scope

This is the public design for an experimental project. No release is available.
The bootstrap package does not supply the functions below.
P0 and P1 have no results at this time.

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
A subsequent contract test must examine that function on each selected HA version.
P4 proposes a public replacement.

## Repositories and release boundary

| Repository | Content | License |
|---|---|---|
| `assist-live` | HA integration, HACS metadata, project documentation | Apache-2.0 |
| `assist-live-server` | Server, PyPI client, containers, HA app | Apache-2.0 |
| `assist-live-esphome` | ESPHome component and example configuration | GPL-3.0 |

The HA app must not go into the HACS repository.
The project selects `2026.10.1` as the HA minimum.
The planned CI matrix tests HA `2026.10.1` and `2026.10.3`.
The baseline runtime is stock HA `2026.10.3`.
These versions are design targets, not a compatibility claim.

The domain is `assist_live`.
The project selects `integration_type: service` and `iot_class: local_push` for the manifest.
The HA minimum and those manifest values are project design decisions, not requirements from the frozen spec.
The initial manifest has `requirements: []` because the client has no PyPI release.

The release manifest must pin `assist-live-client==<exact version>`.
That version must be on PyPI before the integration release.
No bootstrap test imports or installs the unpublished client.

## Components and turn flow

The integration supplies one STT entity and one TTS entity for each satellite.
Each satellite selects a pipeline with its own entities.
HA does not give these entities a run identifier or satellite identifier.
The entity-to-satellite mapping supplies that context without private HA interfaces.
A repair issue reports two satellites that select the same assist-live pipeline.

HA 2026.10.3 exports `async_create_default_pipeline` and `async_update_pipeline` in `assist_pipeline`.
The integration uses them to make and update the pipeline of each satellite, with the TTS voice.
If HA refuses the creation, the config flow gives manual instructions.

The planned config flow selects the server, token, satellites, providers, turn settings, voices, and fallback entities.
The server uses a versioned WebSocket protocol with JSON control messages and binary audio frames.
The first message selects the protocol version and functions.
Client and server contract tests must agree on that protocol.
The server uses no HA administrator credential. Its HA app does not request HA API access.

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

## End-of-turn and STT fallback

The STT entity keeps `requires_external_vad=False` constant on each path.
HA reads the value two times in a run, so server health must not change it.
The server uses Silero VAD, Smart Turn v3.2, and a check for words that cannot end a command.
The server ends a turn before the maximum time only with all three conditions:

- The time without voice is more than the minimum (initial default: 400 ms).
- The Smart Turn score is more than its limit.
- The transcript does not end with words that cannot complete a command.

If those conditions do not apply, the turn ends at the maximum time without voice (initial default: 1.5 s).
The `safe` and `fast` settings keep all three conditions.
P1 selects their values with human recordings.
If `safe` does not satisfy the executable-prefix test, it uses only the maximum time without voice.

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

Configuration checks the fallback language, format, codec, sample rate, and `channels` count.
An assist-live entity cannot be its own fallback.

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

## Provider and license selection

Parakeet TDT 0.6B v2 through `onnx-asr` stays the local STT default until a streaming model satisfies replay tests.
It decodes the full buffer at intervals. CC-BY-4.0 attribution is necessary.
Larger sherpa-onnx Zipformer and NVIDIA streaming FastConformer models are candidates, not approved defaults.
Deepgram Nova-3 live is the cloud STT candidate.

Each request must set `mip_opt_out=true`.

The provider keeps request metadata for 90 days.

ElevenLabs `eleven_flash_v2_5` is the planned cloud TTS default.
Its WebSocket path uses a minimum 50-character chunk schedule and an end-of-reply flush.
The default 120-character schedule adds buffering after HA's 60-character threshold.

Piper 1.8.0 with the `en_US-lessac-medium` voice is the planned local TTS default.
A measurement on the development host selected it from five engine configurations.
The `assist-live-server` record `docs/research/q7t-local-tts.md` gives the results.
Full-file engines are fallback only.

A license does not stop the selection of a model, voice, runtime, or library.
Each component has a record with its pinned source, SHA-256, license, and attribution.
The project code has the Apache-2.0 license.
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
- A follow-up less than 10 s after the previous turn connects to that turn.
- A turn closes at `playback_end` or after 120 s, not merely when the satellite reports `idle`.
- Device events wait for a turn of the same satellite for 5 s, then become `unmatched`.
- HA restart does not keep open turns. Their records stay not completed.

The record includes wake, microphone, voice, turn decisions, transcripts, speaker results, TTS, playback, state, fallback, and failure events.
It also includes superseded, unmatched, barge-in, and all late-result states.
Reports count unmatched events and show the TTS-only turns that P0 completeness measurements do not include.
The server stores timing data in SQLite for 30 days by default and supplies metrics and JSON Lines export.
It does not store audio. Transcript storage uses an explicit option.

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
| P0: measurement | A minimum of 95% of replay voice turns have each event from wake to playback end. Calibrate playback proxies against an external microphone on a minimum of 50 replies. Report p50 and p90 error and baseline time for each step. A proxy has p90 error at most 100 ms. Replay includes device wake and timing events, not only a pipeline WebSocket call. |
| P1: fast turns | On the same hardware, each reply group has a minimum of 40% less p50 last-voice-to-playback time than P0. Pause cuts are at most 3%. The high end of the 95% confidence interval is at most 6%. Correct transcript and intent are a minimum of 95%. Word error rate increases at most one point from P0. The safe setting has 0 executable-prefix cuts and causes 0 incorrect device operations. |
| P1: faults | Test 10 server stops before a turn, during voice, during a pause, and after the complete transcript. Each gives the correct transcript or an empty result with no device operation. |
| P2: conversation | Test 10 faults of each type. Types: restart in `queued`, restart in `delivering`, and restart after `notify`. Also test wake 0 to 500 ms before announce, busy satellite, server restart, `notify` failure, and stop word. Each test gives the specified states and no second announcement. The `notify` call starts in less than 5 s after expiry. Report commands stopped by announcements. |
| P3: barge-in | Complete the firmware design before the hardware test. Find a minimum of 9 of 10 interruptions in less than 500 ms. Keep the first command word and conversation ID. Cause 0 incorrect device operations and at most one incorrect interruption in 10 minutes. Keep wake, stop, timers, announce, and mute functional. If results are unsatisfactory, keep barge-in off and document the stop-word method. |
| P4: upstream | Use `partial` transcripts only after HA releases the interface. Propose public run context, idle-only announce, public TTS lookup, and barge-in interfaces. |

The P3 firmware design must specify the echo-cancelled microphone path and the use of interruption audio and unheard reply text.

## Open checks and quality limits

P0 must select device transport and calibrate playback markers.
It must check HA Marketplace rules on a released HA version before an acceptance claim.
Model cost on target hardware stays open.
P1 must compare streaming STT candidates with Parakeet on human recordings.

CI must subsequently include lint, type checks, HA tests, hassfest, HACS validation, protocol tests, and secret checks.
ESPHome must have a compile check in its repository.
A compiled firmware release must have a GPL source-obligation review.

See [upstream contracts and prototype limits](research/contracts.md).
