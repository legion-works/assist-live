# TTS fallback

The integration is experimental and has no release.

## Text and voice

The entity reads each HA text part one time and sends it to the server without a full-reply wait.
It keeps a copy for fallback. The copy has a 16,384-character limit, not a byte limit.
Empty parts do not increase the copy.
Above this limit, the entity marks `tts_failed` and gives `HomeAssistantError`.
It does not cut the reply or retry it.

The client owns the protocol message size checks.
A part that cannot fit a protocol message starts fallback before server audio.

The pipeline selects the voice through the `voice` option.
The entity sends that value unchanged.
Only a missing option uses the fallback engine's advertised default voice.

## Server and fallback

The health check has a 200 ms limit, different from turn setup.
The server path uses the two flags `healthy=true` and `capabilities.tts=true`.
An older health response without `capabilities` selects fallback.
STT uses the same rule with `capabilities.stt`.

Before server audio, a failure starts fallback with a new text generator.
The generator supplies the kept parts and then the new parts, each one time.
A server format without audio also starts fallback.
An engine with streaming input can supply audio before HA finishes the text.
For other engines, the public HA adapter waits for the full text and makes the full audio file.

After server audio, continuation is possible only with a server that advertises `capabilities.text_positions=true` in its health response.
The entity requests positions only when that flag is `true`.
Each `sentence_end` can give `text_end`, a Unicode code-point count from the start of the request text.
The entity finishes the text input and gives the fallback `full_text[text_end:].lstrip()` from the last marker.
It does not find sentence boundaries itself.
See the [server protocol](https://github.com/legion-works/assist-live-server/blob/f6ac2d61da342dc962db446d9e9d5fab8f80ee95/docs/protocol.md) at the pinned client revision `f6ac2d61da342dc962db446d9e9d5fab8f80ee95` for position rules.

An older server without `text_positions` uses the failure path with the full text, not continuation.
The same rule applies when no marker comes or the last marker has no `text_end`.
The client refuses a position greater than the sent text length with a protocol error.
If no previous position is available, this error uses the failure path with the full text.

Continuation is possible only with the same codec, sample rate, sample width, and `channel` count.
The entity reads the fallback response extension and WAV header to check these values.
Only PCM WAV with the same format can continue.
The entity removes the fallback WAV header before it appends the PCM.
Other formats, invalid headers, and format differences stop the audio.

The WAV parser skips other RIFF chunks before `data`.
It uses only `fmt` and `data` to check the format and supply PCM.

If no sentence position is available, the entity stops the audio.
A restart could play the start of a sentence again. A skip could remove its remaining text.
This condition uses the same failure event and optional notification as a format difference.
The integration does not run the conversation or the device operation again.

## Failure events and notifications

An unsuccessful continuation marks `tts_failed` and fires `assist_live_tts_failed`.
The event data contains `satellite` and the full reply in `text`.
The full reply is not part of diagnostics or timing records.

If the server stops after all sentences are complete, the listener heard the full reply.
The integration counts this condition in the diagnostics counter `late_server_failures`.
It keeps the type-only error log, but it does not mark `tts_failed`, fire the failure event, or send a notification.

The config flow has a `notify_tts_failure` setting, with a default of `false`.
When this setting is on, an unsuccessful continuation calls the public `persistent_notification.create` service.
The call uses `message` for the full reply and `title` for `assist-live TTS failed`.
It supplies no target and no `notification_id`. HA makes a different notification for each failure.

The notification stays in the HA frontend until an operator dismisses it.
This option does not add a dependency or send the text to an external notification provider.

The public interface is `hass.services.async_call(domain, service, service_data, blocking=True)`.
The domain is `persistent_notification`. The service is `create`.
See the [HA service documentation](https://www.home-assistant.io/actions/persistent_notification.create/).

## Diagnostics and logs

Diagnostics give the protocol version, failure counts, and the number of TTS entities in each fallback mode.
The modes are `streaming` and `full_file`.
The counts start at integration setup and include `fallback_used`, `stt_failed`, and `tts_failed`.
The `late_server_failures` counter also starts at integration setup.
Diagnostics contain no text, transcript, token, URL, or audio.
Failure logs contain the error type only, not its message or traceback.
