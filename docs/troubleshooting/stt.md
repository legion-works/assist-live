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
The input also has a 15 s time limit, with time without audio.
The integration does not save this audio to disk.

On fallback, the local engine gets the kept bytes and then the live bytes.
Each byte occurs one time, in order.
Local `pymicro-vad` receives the full turn history.
It ends input after voice and then 0.7 s without voice, or at the input limit.
This constant interval does not use the pipeline voice detector setting.

The fallback deadline is 20 s from the initial turn start.
The health check, server connection, and server audio do not restart this deadline.
Cancellation stops the input task and closes the source.

## Engine selection and failures

Select a standard HA STT entity as the fallback.
The entity must accept the stream language, format, codec, sample rate, bit depth, and `channel` count.
The integration checks these properties before input and again before fallback.
It refuses an assist-live entity in the fallback chain and refuses a circular chain.

The turn record uses `fallback_used` when local fallback starts.
A complete transcript uses `stt_final`.
If fallback has a failure or the time limit ends, the result has empty text and the record uses `stt_failed`.
After a failure, the integration does not supply command text that is not complete.
It does not retry the fallback or run the command again.

If fallback has a failure, check the engine language and audio properties first.
Then check server connectivity and the standard engine logs.
Do not put tokens or command text in a public issue.
