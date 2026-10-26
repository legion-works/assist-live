# Voice forwarding setup

The integration is experimental and has no release.
Do not use it for safety-critical controls.

## Server and standard engines

1. Configure a standard HA STT entity and TTS entity.
2. Provision one anonymous server key for each satellite device.
3. Give the integration token access to those keys.
4. Add the assist-live integration. Record the server URL and integration token.
5. Select the standard fallback engines. An assist-live engine cannot be a fallback.
6. For each device, select its provisioned key and its pipeline selector.

Use TLS or an isolated network for the server access token.
HTTP sends the token without encryption. The config flow shows a warning before an HTTP connection.
The warning lets you continue for an isolated network. It does not refuse HTTP.
An invalid URL has a different error from a connection failure.

Use each device and key only one time in an entry.
Use no more than five satellites with one token.
The server has 16 audio and event connection slots for each token.
Each satellite can use one event connection and two audio connections.

The manifest pins `assist-live-client==0.0.0`.
That client is not on PyPI at this time.
Local tests and CI use the Git source and commit in `uv.lock`.
The approved client release is necessary for installation through HA.
Do not replace the manifest requirement with a local path or Git URL.

## Pipelines and voices

The integration adds an STT entity and a TTS entity for each satellite.
It uses the public pipeline setup functions after those entities are available.
Each new pipeline uses the built-in HA conversation agent.
The integration saves the pipeline ID in its config entry.
It does not change an unrelated pipeline or the preferred pipeline.

1. Open the voice assistant settings in HA.
2. Find the pipeline with the anonymous satellite key in its name.
3. Select the conversation agent, language, and TTS voice for that pipeline.
4. Select that pipeline in the pipeline selector of the same satellite.
5. Do these steps again with a different pipeline for each satellite.

The TTS entity supplies the standard engine voices as HA `Voice` objects.
The selected pipeline voice goes unchanged to the server and fallback engine.
Do not select one assist-live pipeline on two satellites.
A repair issue shows when two selectors use the same owned pipeline.

If automatic setup gives no pipeline, make one in the HA voice assistant settings.
Select the STT and TTS entities of the same satellite.
Use the full procedure in [Pipelines for each satellite](research/q2-pipelines.md).

If a saved pipeline ID is missing, repair that pipeline or remove the entry after operator approval.
The integration does not make a replacement for a missing saved ID.
After a creation stops, check the pipeline list before you try setup again.
Keep the saved ID when the pipeline is in the list.

## Fallback behavior

STT always reports `requires_external_vad=False` through its `audio_processing` property.
It checks server health with a 200 ms limit.
When the server is unavailable, `pymicro-vad` ends the local stream after 0.7 s without voice.
This interval starts after voice detection.

All STT input stops at 15 s or 480,000 bytes of mono 16-bit PCM at 16 kHz.
The fallback has 20 s from the start of the request.
Before audio input, the integration checks language, format, codec, sample rate, bit depth, and `channel` compatibility.
It gives an error result if the fallback is not compatible or has a failure.
It does not retry that fallback.

A different input task keeps the audio when the server sender stops.
The fallback reads the kept audio first, then the remaining live audio.
The integration does not save audio to disk.

TTS keeps each text part and supplies a new generator to the fallback engine.
This also works when a sender stops after it reads part of the text.
Standard engines without streaming input receive the full text through the HA adapter.
Server PCM has a streaming WAV header for mono, 16-bit samples at the declared sample rate (16000 Hz or 22050 Hz).
The sample rate stays the same in one stream.
If the server supplies no audio, the integration uses the fallback engine.

The text buffer has a 16,384-character limit. Empty parts do not increase the buffer.
If the text is more than that limit, the request stops with `tts_failed` and an HA error.
The integration does not silently cut the text or retry it.

The client supplies sentence boundaries to the integration.
If server audio stops after playback data starts, the integration stops that response.
It does not say the same words again or run the conversation again.
It fires `assist_live_tts_failed` with `satellite` and the full reply `text`.
An HA automation can send that event text to a selected notification service.
The integration does not continue a reply at a sentence boundary at this time.

## Turn records and repairs

Each STT request opens a UUIDv7 voice turn for its bound key.
TTS uses a finalized voice turn or opens a `tts_only` turn.
A new STT request or announcement supersedes an unfinished turn of the same satellite only.
A follow-up in less than 10 s uses the previous turn as its parent.
Unavailable conversation IDs stay null.

The integration starts one event subscription for each key before the forwarding entities become available.
The subscription starts again with its last event cursor after a connection failure.

The HA store keeps one cursor for each satellite key, with the open turn IDs.
Cursor-only writes occur at most one time in 5 s. Turn changes save immediately before audio forwarding.
When HA removes the entry from service, it saves pending cursor changes.
A sudden process failure can replay the pending cursor batch.
The next 1 s maintenance tick writes the batch after the 5 s interval.

The protocol has no subscription-ready acknowledgement.
Stored event replay supplies events during connection setup.

Only a device `playback_end` with the correct turn ID or the 120 s deadline closes a turn.
The public HA `idle` state does not close it.
HA state observations use proxy quality. Integration timestamps use measured quality.
The server checks record completeness from the necessary `stage` values.

The integration saves the open turn IDs before audio forwarding.
After an HA restart, those IDs receive `turn_superseded` with `reason=restart`.
They do not resume or get a complete status.
A server outage can prevent these markers.
The server deadline then gives the previous turn an `incomplete` status.

An unavailable server shows a repair issue.
Check its URL, token, provisioned keys, and process status.
STT endpoint settings do not change with server health.
Offline fallback observations stay local when the record server is unavailable.

The ledger deadline tick runs at 1 s intervals, but unchanged state does not cause a store write.
Server health checks run at 30 s intervals when healthy and 5 s intervals after an unsatisfactory health result.
Each request has its 200 ms health check.

After three event or store failures with no good event between them, a repair issue identifies the satellite key.
Each retry has a debug log without tokens, reply text, or the error message.
Retry intervals are 1, 2, 4, 8, 16, then 30 s. A good event clears the failure count and repair.
A refused event does not increase the cursor. Repair the stream or storage to remove the cause.

Closed local records stay for at most 10 s, with a maximum of 128 closed records for each entry.
The 10 s interval includes the parent-turn window and the server's 5 s reorder window.
The integration also removes the connected IDs and pending markers of those records.
The server, not the local cache, keeps the historical records.

Device trials have not run. No test hardware is available at this time.
