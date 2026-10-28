# Configuration

The project is experimental and has no release.

## HA options

Select the server URL, access token, fallback STT entity, and fallback TTS entity.
Use one pipeline for each satellite.
Select the voice in the HA pipeline TTS options.
The integration sends that voice to the client without a change.

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

## Server providers

The [server configuration](https://github.com/legion-works/assist-live-server/blob/f6ac2d61da342dc962db446d9e9d5fab8f80ee95/docs/configuration.md) gives the provider selection rules.
Parakeet and Piper are local providers. Deepgram and ElevenLabs are cloud providers.
The local Piper voice is `en_US-lessac-medium`, with PCM audio at 22050 Hz.

Piper buffers each sentence before synthesis. It streams the sentence audio to HA.
It does not make audio for each text token.

The pinned protocol does not show the server turn `profile` through health.
HA cannot read or select a server turn `profile` through this protocol.
HA diagnostics show the last health values as `server_local_only` and `server_destinations`.
Missing health fields show as `not_reported`. The destination map also includes the configured speaker plug-in.
Diagnostics show the actual voice and audio format when HA has them.

## Fallback and timing

The STT fallback must accept the language, PCM codec, 16-bit samples, 16000 Hz, and `channel=1`.
The TTS fallback must accept the language and voice that the pipeline selects.
A streaming fallback can start audio before HA gets all text.
A non-streaming fallback buffers the full text. Diagnostics show this path as `full_file`.

Mid-turn continuation must also use the same audio format. See the [TTS rules](troubleshooting/tts.md).

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
