# STT fallback

The integration is experimental and has no release.
Do not use it for safety-critical controls.

## Server connection

Each turn has one server health check with a 200 ms limit.
An unsatisfactory check selects the local path for that turn.
The integration does not change `SpeechAudioProcessing` during the turn.
The `requires_external_vad` value stays `False`.

The server sends an authenticated heartbeat each 100 ms, also without voice.
A closed connection or 600 ms without an authenticated server message starts local fallback.
Local audio does not restart this timer.
A complete server transcript prevents fallback, also after a connection error.

## Audio and time limits

The integration keeps a maximum of 480,000 bytes in memory for each turn.
This is 15 s of mono, 16-bit PCM at 16 kHz.
The input limit is 15 s of audio input from the start of the turn.
This limit also includes time without audio.
The integration does not save this audio to disk.

On fallback, the local engine gets the kept bytes and then the live bytes.
Each byte occurs one time, in order.
Local `pymicro-vad` receives the full turn history.
It ends input after voice and then 0.7 s without voice, or at the input limit.
This constant interval does not use the pipeline voice detector setting.

The fallback deadline is 20 s from the initial turn start.
The health check, server connection, and server audio do not restart this deadline.
Cancellation stops the input task and closes the source.

The STT result does not wait for timing events, also when the server stops during voice input.
Each background event request has a 2 s limit. After a send failure, events stay pending until a satisfactory maintenance health check.
If the 512-event list is full, the integration removes the oldest event, and `timing_events_dropped` increases.
Missing timing events do not cause a command to run again.

## Engine selection and failures

Select a standard HA STT entity as the fallback.
The entity must accept the stream language, format, codec, sample rate, bit depth, and `channel` count.
The integration checks these properties before input and again before fallback.
It refuses an assist-live entity in the fallback chain and refuses a circular chain.

The fallback can start after assist-live or go out of service during an integration reload.
The STT entity finds the current fallback by entity ID for each request and before fallback.
While it is missing, the entity keeps its last known languages in HA storage, also across a restart.
Without stored or known languages, it shows no languages. HA then refuses pipeline language validation rather than using an unknown language.
When the engine is available again, its current languages replace the stored list.

The repair issue `fallback_stt_unavailable` names a missing engine.
Check that engine's integration. The issue clears on the next engine lookup or due maintenance health read after recovery.
If the turn must use the missing fallback, its result has empty text and an error state.
The ledger records `fallback_unavailable` and `stt_failed`. A healthy server can supply a complete transcript without fallback.

Property reads do not make a missing-engine repair issue.
Maintenance makes it only with HA state `CoreState.running`, not during startup.
A turn that must use the missing fallback makes it, also during startup.

The turn record uses `fallback_used` when local fallback starts.
The server records `stt_final` for its complete transcript. The integration records `stt_final_received` when HA gets that transcript.
On fallback, the integration records `stt_final` and a proxy `end_decision` with `decision_source: ha_audio_end`.

If fallback has a failure or the time limit ends, the result has empty text and the record uses `stt_failed`.
The integration sends that timing event only after the server accepts `fallback_used` with `part: stt` for the turn.
Without that accepted marker, the failure stays in local diagnostics, also before the integration adds a destination row.
After a failure, the integration does not supply command text that is not complete.
It does not retry the fallback or run the command again.

If fallback has a failure, check the engine language and audio properties first.
Then check server connectivity and the standard engine logs.
Do not put tokens or command text in a public issue.
