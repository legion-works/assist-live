# Use assist-live

The integration is experimental and has no release. See [setup](setup.md) for prerequisites and installation.

## Use cases

- Keep your HA conversation agent and native satellite controls. Use the server to stream replies without a replacement for the Assist pipeline.
- Configure standard HA voice engines as fallbacks. If the server is not available, a turn can use the necessary fallback to get a transcript or a reply.
- Compare reply times before and after a change to your pipeline. Use the latency sensor for each satellite to see the interval from the end decision to playback start.

## Entities and functions

The integration adds these three entities to each satellite that you select during [setup](setup.md#devices). It does not add a new satellite entity.

| Entity | Function |
|---|---|
| STT (`stt`) | Sends microphone audio to the server. Supplies the transcript to the selected HA conversation agent through Assist. Uses the configured STT fallback if it cannot use the server. |
| TTS (`tts`) | Sends reply text to the server and streams the audio back to HA. Uses the configured TTS fallback if necessary. Select the server voice in the pipeline. |
| Last turn latency (`sensor`) | Shows milliseconds from the end decision to playback start for the latest turn. The `quality` attribute shows `measured`, `proxy`, or `missing`. A missing or invalid interval has no numeric value. |

See [configuration](configuration.md) for voice selection, fallback options, and name hints. See [latency attributes](configuration.md#fallback-and-timing) for the timing sources and uncertainty.

### Other functions

- For a configured satellite, the integration adds a [spoken-reply prompt](design.md#spoken-replies) to the Assist LLM API. The prompt tells the agent to use plain spoken sentences and to end with one question if an answer is necessary. It adds no tools. Agents that do not use the Assist API do not get this prompt.
- The integration fires [`assist_live_tts_failed`](troubleshooting/tts.md#failure-events-and-notifications) after a fallback failure or if fallback cannot continue the audio. Event data has `satellite` (the server key) and `text` (the reply text that HA got).
- If the server identifies a speaker by name, the integration fires `assist_live_speaker`. Event data has `turn_id`, `satellite`, `name`, `distance`, and `destination`. See the [privacy rules](privacy.md#content-and-records).

## Data updates

The STT and TTS entities run on request from HA. They do not poll. Each request can check server health with a 200 ms limit.

The server pushes turn events on a subscription for each satellite. The integration also monitors the satellite state in HA. The latency sensor updates when the latest turn or server availability changes. It does not poll. A new turn can have no value until HA gets its timing data.

The integration polls server health at setup and at 30 s intervals after a satisfactory check. After an unsatisfactory check, it uses 5 s intervals. The latency sensor shows `unavailable` during a server outage. The STT and TTS entities can use the fallback engines during that time. The health check intervals have no user setting.

The integration sends [name hint updates](configuration.md#stt-name-hints) 5 s after a registry or exposure change. The list has a 500-name limit.

## Known limits

- Each entry connects to one server with one token. The integration has no automatic discovery for servers or satellites. You can select a maximum of five satellites, only during setup. To change satellites, remove the entry and add it again. Use a different pipeline for each satellite. See the [setup limits](setup.md#server-and-standard-engines) and [removal steps](setup.md#removal).
- Stock ESPHome firmware on Home Assistant Voice Preview Edition starts a 2 s timer when it gives the reply URL to the media player. If playback does not start before the timer ends, the satellite can end the turn before it plays the reply.
- If the server gives a 22050 Hz format but no audio in 1 s, assist-live can start the reply stream with 250 ms of silent audio. The 1 s interval starts with the TTS request. The HA 2026.10 FLAC converter buffers this audio until it gets more audio. Thus the satellite gets no FLAC from the silent audio alone. HA 2026.11 can send FLAC before server audio. See [reply start](design.md#reply-start-before-the-first-server-audio).
- If the server stops during a sentence, TTS fallback can speak the start of that sentence again. Continuation is possible only with a usable server sentence position and PCM WAV audio in the same format. See [TTS fallback](troubleshooting/tts.md#server-and-fallback).
- The [end-of-turn word rules](design.md#end-of-turn-and-stt-fallback) use the HA English sentence templates. These rules cannot tell if a command in a different language is complete. The [local Parakeet TDT 0.6B v2 model](design.md#provider-and-license-selection) transcribes English only. Its [STT name hints](design.md#stt-name-hints) apply only to the `final` transcript. Cloud providers and `partial` transcripts do not use these hints.
- Voice input has a 15 s limit. The TTS reply has a 16,384-character limit. See the [STT input limits](troubleshooting/stt.md#audio-and-time-limits) and [TTS text limits](troubleshooting/tts.md#text-and-voice).
- The local-only option checks configured destinations. It is not a firewall and does not include the conversation agent. See [privacy](privacy.md).

## Examples

In HA, open **Settings > Automations & scenes**. Make a new automation for each example. Open its menu and select **Edit in YAML**. Paste the example into the editor.

### Get a notification for long reply latency

See the [latency sensor configuration](configuration.md#fallback-and-timing) before you use this example. Replace `sensor.room_satellite_last_turn_latency` with the entity ID of your satellite's **Last turn latency** sensor. The automation sends a notification when the value goes above 1500 ms. It does not send one for each value above the limit.

```yaml
alias: Assist reply latency crossed the limit
triggers:
  - trigger: numeric_state
    entity_id: sensor.room_satellite_last_turn_latency
    above: 1500
actions:
  - action: persistent_notification.create
    data:
      title: Assist reply delay
      message: The last reply took more than 1500 ms from the end decision to playback start.
mode: single
```

### Get a TTS failure notification without reply text

This automation monitors [TTS failure events](troubleshooting/tts.md#failure-events-and-notifications) from all configured satellites. The notification has no reply text. To prevent a second notification with the text, keep `Show full reply in a persistent notification if TTS fails` off. You can set this option only during [setup](setup.md#setup-fields).

```yaml
alias: Assist voice reply failed
triggers:
  - trigger: event
    event_type: assist_live_tts_failed
actions:
  - action: persistent_notification.create
    data:
      title: Assist voice reply failed
      message: Check the assist-live server and the fallback TTS engine.
mode: single
```

## Problem checks

### Cannot read provisioned bindings from the server

HA cannot read a usable list of satellite keys for the token. Check that the server is available and that its URL does not redirect. Check that the token has access to a minimum of one key. See [server setup](setup.md#server-and-standard-engines).

### Satellites share an assist-live pipeline

Two satellite selectors resolve to the same pipeline. This can occur if the two selectors use **Preferred**. Select a different assist-live pipeline for each satellite. See [pipeline setup](setup.md#pipelines-and-voices).

### The latency sensor has no number

`Unavailable` means that the last maintenance health check did not succeed or that no check is complete. Check the URL, token, and server status. If the sensor shows `unknown`, HA has no usable interval for the latest turn. Check the timing attributes after a full reply. See [data updates](#data-updates).

### A turn has no transcript or the reply stops

If the fallback is unavailable or cannot use the language or audio format, it cannot complete the request. Read the repair issue if HA shows one. For no transcript, check the [STT engine and audio requirements](troubleshooting/stt.md#engine-selection-and-failures). For a reply that stops, check the [TTS voice and continuation rules](troubleshooting/tts.md#server-and-fallback).
