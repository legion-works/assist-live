# Voice forwarding setup

The integration is experimental and has no release.
Do not use it for safety-critical controls.

## Installation

Before you install the integration, make sure that you have these items:

- HA 2026.10.1 or a subsequent version.
- An assist-live server, its URL, and an integration token. The server is in the `assist-live-server` repository.
- A standard HA STT entity and a standard TTS entity for fallback.
- For each satellite, an HA device with an `assist_satellite` entity and an HA Assist pipeline control.

No release is available. To install the integration from the repository:

1. Copy `custom_components/assist_live/` to the `custom_components/` folder of the HA configuration folder.
2. Restart HA.
3. Do the steps in [Server and standard engines](#server-and-standard-engines).

## Devices

The [satellite reply test](verification.md#reply-start-before-slow-server-audio-on-a-satellite-2026-11-25-utc) used one Home Assistant Voice Preview Edition with stock firmware. It did not test question replies.

The integration can use Assist satellites that have an `assist_satellite` entity and a compatible pipeline control on the same HA device. The pipeline control is a `select` entity with registry translation key `pipeline` or `pipeline_n`. ESPHome, Wyoming, and VoIP satellites can have these entities. These are setup requirements, not test results for other models.

The integration cannot use a device without the two entities. A speaker that only has a `media_player` entity is not an Assist satellite.

## Server and standard engines

1. Configure a standard HA STT entity and TTS entity.
2. Provision one anonymous server key for each satellite device.
3. Give the integration token access to those keys.
4. Open **Settings > Devices & services > Add integration** and select **assist-live**. Record the server URL and integration token.
5. Select the standard fallback engines. An assist-live engine cannot be a fallback.
6. For each device, select its provisioned key and its HA Assist pipeline control. Setup refuses other controls, such as VAD sensitivity and wake word.

Use TLS or an isolated network for the server access token.
HTTP sends the token without encryption. The config flow shows a warning before an HTTP connection.
The warning lets you continue for an isolated network. It does not refuse HTTP.
An invalid URL has a different error from a connection failure.

Use each device ID and each satellite key only one time across all assist-live entries in one HA instance.
If setup shows `This device or key is already in use`, select a device and a provisioned key that no entry uses.
Use no more than five satellites with one token.
Setup ends when you add the fifth satellite.
The server has 16 audio and event connection slots for each token.
Each satellite can use one event connection and two audio connections.

### Setup fields

| Field | Purpose | Default |
|---|---|---|
| `Server URL` | Set the HTTP or HTTPS address of the assist-live server. | None. Necessary. |
| `Access token` | Set the integration token from the server. Give it access to the selected satellite keys. | None. Necessary. |
| `Fallback STT entity` | Select the standard STT entity to use when the server cannot supply STT. | None. Necessary. |
| `Fallback TTS entity` | Select the standard TTS entity to use when the server cannot supply TTS. | None. Necessary. |
| `Show full reply in a persistent notification if TTS fails` | Show the full reply text in an HA notification after a TTS failure. | Off. |
| `Use local destinations only` | Apply the local destination checks for server and fallback engines. See [HA options](configuration.md#ha-options). | Off. |
| `Permit transcript storage on the server` | Let the server store the last transcript of the turn if its policy also lets it do this. | Off. |
| `Record an unknown fallback STT destination as local` | Identify the selected STT entity as local if HA cannot find its destination. Check the entity before you select this field. | Off. |
| `Record an unknown fallback TTS destination as local` | Identify the selected TTS entity as local if HA cannot find its destination. Check the entity before you select this field. | Off. |
| `Satellite device` | Select the HA device of the voice satellite. | None. Necessary. |
| `Provisioned satellite key` | Select the anonymous key from the server for this device. | None. Necessary. |
| `Satellite pipeline selector` | Select the HA Assist pipeline control of this device. | None. Necessary. |
| `Add another satellite` | Add one more satellite before you complete setup. | Off. |

The HTTP warning has no fields. Select **Submit** only on an isolated network.

Use **Reconfigure** to change the server URL.
The server at the new URL must accept the current token for all configured satellite keys.
HA cannot tell apart two servers that have the same token and keys.

The manifest pins the published pre-release `assist-live-client==0.1.0a3` from PyPI.
HA, local tests, and CI install that same client version.
The integration itself has no release.
Do not replace the manifest requirement with a local path or Git URL.

The client does not follow redirects for HTTP requests or WebSocket handshakes.
The client also refuses a WebSocket redirect when the scheme, host, and port do not change.
Use the server URL that does not give a redirect.

## Reauthentication

If the server rejects the saved access token, HA shows a reauthentication issue in **Settings > System > Repairs**.
The entry stays loaded. The configured fallback engines answer turns until you replace the token.

1. Make a replacement integration token on the same server.
2. Give the token access to all satellite keys in this entry.
3. Open the reauthentication issue. Record the replacement token and select **Submit**.

Do not use a token from a different entry on the same server.
After the check, HA saves the token and reloads the entry. The server URL and satellite keys do not change.
If the check is unsatisfactory, see [Invalid access token](troubleshooting/invalid-token.md).

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

The TTS entity supplies server voices as HA `Voice` objects.
The selected pipeline voice goes unchanged to the server.
Select `fallback_tts_voice` independently in the integration options, or use the fallback engine's default voice.
Do not select one assist-live pipeline on two satellites.
A repair issue shows when two configured selectors use the same assist-live pipeline, also across entries.
This check includes the **Preferred** option.

If automatic setup gives no pipeline, make one in the HA voice assistant settings.
Select the STT and TTS entities of the same satellite.
Use the full procedure in [Pipelines for each satellite](research/q2-pipelines.md).

If a saved pipeline ID is missing, repair that pipeline or remove the entry after operator approval.
The integration does not make a replacement for a missing saved ID.
After a creation stops, check the pipeline list before you try setup again.
Keep the saved ID when the pipeline is in the list.

## Fallback behavior

With local only on, HA uses server STT or TTS only when health reports `local_only=true` and `local` for that part.
If a check is unsatisfactory, HA uses the fallback. With local only on, HA does not use an older server without these health fields.
These configured-destination checks are not a firewall. These checks do not include the conversation agent.

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

Continuation is possible only with a server that advertises `capabilities.text_positions=true` in its health response.
The entity requests positions only when that flag is `true`.
If server audio stops, the fallback can continue from the last `sentence_end.text_end` when the audio formats agree.
The position counts Unicode code points. The entity removes whitespace at the start of the remaining text.
See the [TTS fallback rules](troubleshooting/tts.md) for format checks.

An older server without `text_positions` uses the failure path with the full text, not continuation.
No marker or a last marker without `text_end` also selects that failure path.
The integration stops the audio and fires `assist_live_tts_failed` with `satellite` and the full reply `text`.
An HA automation can send that event text to a selected notification service.
The integration does not run the conversation again.

## Turn records and repairs

Each STT request opens a UUIDv7 voice turn for its bound key.
TTS uses a finalized voice turn or opens a `tts_only` turn.
A new STT request or announcement supersedes an unfinished turn of the same satellite only.

A follow-up uses the previous turn as its parent only if the server opened that turn and it closed with `playback_end`.
The new turn must open less than 10 s after HA received that event.
At 10 s, it has no parent. A superseded turn cannot become a parent.
The 120 s limit and HA restart also prevent a turn from becoming a parent.
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

An unavailable server shows a repair issue. A rejected token starts reauthentication.
Check its URL, token, provisioned keys, and process status.
STT endpoint settings do not change with server health.
Offline fallback observations stay local when the record server is unavailable.

The ledger deadline tick runs at 1 s intervals, but unchanged state does not cause a store write.
Server health checks run at 30 s intervals when healthy and 5 s intervals after an unsatisfactory health result.
If the server rejects the token, new server requests stop until HA reloads the entry.
Each request has its 200 ms health check.

After three event or store failures with no good event between them, a repair issue identifies the satellite key.
Each retry has a debug log without tokens, reply text, or the error message.
Retry intervals are 1, 2, 4, 8, 16, then 30 s. A good event clears the failure count and repair.
A refused event does not increase the cursor. Repair the stream or storage to remove the cause.

Closed local records stay for at most 10 s, with a maximum of 128 closed records for each entry.
The 10 s interval includes the parent-turn window and the server's 5 s reorder window.
The integration also removes the connected IDs and pending markers of those records.
The server, not the local cache, keeps the historical records.

No P0 or P1 acceptance result is available in this repository. These procedures are not device-trial results.

## Removal

To delete one entry:

1. Open **Settings > Devices & services > assist-live**.
2. Open the menu of the entry and select **Delete**.

HA removes the STT, TTS, and latency sensor entities of that entry.
The integration stops the event subscriptions and closes the server connections of that entry.
The other entries continue to operate.

When HA stops or reloads an entry, the integration writes its turn store.
If that write is not satisfactory, the integration keeps the turn state in memory for the next setup of the entry. An HA restart clears this state from memory.

To remove the integration files, first delete each assist-live entry.
Then remove the `custom_components/assist_live/` folder and restart HA.

These items stay after you delete an entry:

- The pipelines that the integration made. They keep the IDs of the removed engines. Delete them in the voice assistant settings, or select different engines. Select a different pipeline on each satellite that uses one of them.
- The turn store file `.storage/assist_live.<entry ID>.turns` in the HA configuration folder. It contains open turn IDs and event cursors. You can delete it after you delete the entry.
- The server records and the integration token. If no other HA entry uses the token, remove it from the server configuration.
