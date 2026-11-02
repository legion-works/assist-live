# Configuration

The project is experimental and has no release.

## HA options

Select the server URL, access token, fallback STT entity, and fallback TTS entity.
Use one pipeline for each satellite.
Select the voice in the HA pipeline TTS options.
The entity shows the voices and default voice from the latest server health response.
The integration sends an explicit voice to the server without a change.

Without a voice option, it sends no voice. The server uses its default voice.
An older server without a voice list gives no voice selections or default option.

In the integration options, select `fallback_tts_voice` from the fallback engine's voice list.
The empty selection uses that engine's default voice.
Entries without this option use that default until an operator selects a voice.
The integration does not send the fallback voice to the server or the server voice to the fallback.

To change fallback engines after setup, open **Settings > Devices & services > assist-live > Configure**.
Select the fallback STT and TTS entities, then select **Submit**.
If the TTS entity changes, a second step shows the voices of the new engine and **Default**.
Select a voice, then select **Submit** again to save all changes.
If the TTS entity does not change, the first step also shows the voice selection.

HA refuses missing engines and assist-live entities as fallbacks.
The same destination checks apply at setup and in the options flow.
A local decision stays with its entity ID, not with the STT or TTS field.
The integration removes decisions for entities that are not selected.

A fallback entity change reloads the integration. The next turn uses the new engines.
A mode or voice change without an entity change does not reload the integration or stop the current turn.
See the [fallback configuration contract](design.md#components-and-turn-flow).

The `local_only` option defaults to `false` in HA.
When it is on, HA refuses cloud fallback destinations before use.
An unknown fallback also must have an explicit local decision in the options flow.
Diagnostics show that decision and its UTC time.
A known cloud integration cannot use a local decision.
HA reads the fallback integration's `iot_class` again before each fallback request.

| `iot_class` | Destination |
|---|---|
| Starts with `local` | `local` |
| Starts with `cloud` | `cloud` |
| Missing or a different value | `unknown` |

With local only on, HA uses server STT or TTS only when health reports `local_only=true` and `local` for that part.
If a check is unsatisfactory, HA uses the fallback. This includes older servers without these health fields.

Set `local_only` on the server too. These configured-destination checks are not a firewall.
These checks do not include the conversation agent.
See [privacy](privacy.md) for the limits of these checks.

## STT name hints

The integration sends the names and aliases that Assist can use, and the area names, for each configured satellite.
It changes each name to lower case and removes spaces at the two ends.
Each name has 1 to 64 characters and a minimum of one letter. The list has no duplicates.
Sorted area names come first, then sorted entity names and aliases. The integration keeps the first 500 names.

It sends the list at setup and 5 s after an entity registry, area registry, or Assist exposure change.
These three change sources use one debounce timer.
A different digest in server health also starts a send for that satellite, for example after a server restart.
After a server restart, the integration sends the list again on its next maintenance health read.
Only setup and maintenance health reads start this recovery. A turn does not wait for the list.

The integration sends names only when health reports `stt_hints=true`. Older servers and cloud STT providers get no list.

Each send has a 2 s limit. A failure does not stop a turn. The next change or health check can retry it.

Diagnostics give each satellite's `count`, `digest`, `not_sent`, `last_result`, and `last_send` in `stt_hints`.
The result is `sent`, `skipped_no_support`, or `failed`. The time is the last satisfactory send in UTC, or `null` before one succeeds.
The `stt_hints_failures` counter starts at integration setup. Logs give only the error class, not the names or server error text.
See [privacy](privacy.md#stt-name-hints) for the destination and storage rules.

## Conversation agents

Enable the Assist API ("Device control") on the conversation agent.
For a satellite of a loaded integration entry, the integration gives this API a prompt part and no tools.
The prompt tells the agent to use plain spoken replies and end with a question when an answer is necessary.
See the [spoken reply rules](design.md#spoken-replies).

Agents that do not use the Assist API do not get this prompt part.
This includes an integration that builds its own LLM API or an external agent behind a bridge.
For these agents, use `extra_system_prompt` or the agent's own prompt settings to set the same rules.
The [server text rules](design.md#spoken-text) apply to all agents.

## Server providers

The [server configuration](https://github.com/legion-works/assist-live-server/blob/8339feea795eac71008cbaa3f8046706a14602b6/docs/configuration.md) gives the provider selection rules.
Parakeet and Piper are local providers. Deepgram and ElevenLabs are cloud providers.
The local Piper voice is `en_US-lessac-medium`, with PCM audio at 22050 Hz.

Piper buffers each sentence before synthesis. It streams the sentence audio to HA.
It does not make audio for each text token.

The pinned protocol does not show the server turn `profile` through health.
HA cannot read or select a server turn `profile` through this protocol.

HA diagnostics show current server health as `server_healthy`.
The field `server_last_healthy` gives the UTC ISO time of the last satisfactory health check, or `null` before one succeeds.
The group `server_last_known` contains `local_only`, `destinations`, `tts_voice_count`, and `tts_default_voice` from that check.
It is `null` before a satisfactory check. Its values stay unchanged during an outage.

Missing local-only or destination fields show as `not_reported`. The destination map also includes the configured speaker plug-in.
Diagnostics show the actual voice and audio format when HA has them.

The voice count and default voice contain no reply text.

## Fallback and timing

Timing events use one background sender. A turn does not wait for timing events.
Each request has a 2 s limit, and the pending list has a 512-event limit.
After a failure, the sender retries only after a new satisfactory maintenance health check.
The maintenance health interval is 30 s after success and 5 s after failure.

The diagnostic counter `timing_events_dropped` counts events removed when the list is full or the server refuses them.
It starts at integration setup. These limits have no user options.
The counter `turn_opens_dropped` counts removed refused opens, not timing events.
Integration shutdown discards pending events without a last send. Pending events do not survive reload or restart.

A refused event does not stop the next event. Time limits and connection errors keep the event for the next health check.
After an outage, the sender opens each pending turn before its events, with the initial `opened_utc_ns` time.

A retry with the same open values succeeds if HA does not receive the first response.
The server refuses a conflicting or too-old open. The sender then sends each event for its own check.
It does not send the refused open again.

The STT and TTS entities keep `conversation_id` empty. Each timing event has its `turn_id`.

The STT fallback must accept the language, PCM codec, 16-bit samples, 16000 Hz, and `channel=1`.
The TTS fallback must accept the language. It uses its own selected or default voice.
A streaming fallback can start audio before HA gets all text.
A non-streaming fallback buffers the full text. Diagnostics show this path as `full_file`.

Mid-turn continuation must also use the same audio format. See the [TTS rules](troubleshooting/tts.md).

Each `fallback_used` event has a `reason` and a `part` (`stt` or `tts`). Diagnostics count each `reason` in `fallback_reasons`.
A debug log gives that value and the error class, or `None` when no error occurred.
The log does not contain error messages, addresses, tokens, transcripts, or reply text.

| Reason | Cause |
|---|---|
| `health_timeout` | The health deadline expired. |
| `health_error` | The health request did not complete correctly. |
| `server_unhealthy` | Health reports an unhealthy server. |
| `stt_unsupported` | The server does not report an STT function. |
| `tts_unsupported` | The server does not report a TTS function. |
| `not_local_only` | The server does not report local-only mode. |
| `destination_not_local` | The selected server part does not report a local destination. |
| `open_refused` | The server refused to open the turn. |
| `open_error` | A different error prevented the turn from opening. |
| `stream_refused` | The server refused the stream request. |
| `stream_closed` | The stream ended before a usable result. |
| `stream_error` | The stream data or audio is not usable. |
| `message_timeout` | The message deadline expired. |

The policy records keep the names `server_not_local_only` and `server_destination_not_local`.
Their event `reason` values are `not_local_only` and `destination_not_local` in the same order.

Each satellite has one last-turn latency sensor, in milliseconds.
Its attributes show quality, uncertainty, and the end-decision source.
Missing events give no value and quality `missing`.

A server message that HA sees is a proxy until the server timing event supplies its timestamp.
A fallback decision uses source `ha_audio_end` and quality `proxy`.
It is not a server-detector measurement.
Device clock estimates keep their uncertainty and show as proxy quality in the sensor.
The `playback_quality` attribute keeps the initial quality value.

The `turn_id` and `turn_type` attributes identify the latest turn when it opens, before the conversation step.
These diagnostic correlation IDs contain no transcript or audio content.
Each diagnostic turn row includes the latency sensor's `turn_id` attribute value, a correlation ID with no content.
HA does not include these two attributes in recorder history.

## Transcript storage and release checks

The `record_transcripts` option defaults to `false`.
The server stores a `final` transcript only when its policy and the turn option are the two `true` values.
Speaker names go to the turn record and `assist_live_speaker` event only.
They do not change transcript bytes.

The manifest pins `assist-live-client==0.0.0`, the version at the pinned client revision.
No client release is available on PyPI at this time.
Before an approved integration release, run:

```sh
uv run python tools/check_release_pin.py --manifest custom_components/assist_live/manifest.json --pypi-version 0.0.0 --require-published
```

The check refuses a different manifest pin or client package version.
It also refuses a version without a package file on PyPI.
The check does not publish a package.
The manual archive workflow stops before it builds an archive if this check is unsatisfactory. It uploads nothing.
