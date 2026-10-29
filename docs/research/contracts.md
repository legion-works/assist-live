# Upstream contracts

This is a 2026-10-24 research record. See [configuration](../configuration.md) for current integration behavior.

## Design versions

The frozen design targets HA `2026.10.x`, with stock `2026.10.3` as the baseline runtime.
ESPHome `2026.10.0` and Voice PE packages `26.6.0` and `26.9.0` supplied the design facts.
These are design facts and prototype findings, not bootstrap compatibility results.

## HA pipeline

- An STT entity with `requires_external_vad=False` through `SpeechAudioProcessing` owns the end-of-turn decision.
- HA reads that value two times during a run. The entity must keep it constant.
- `run-start` gives the TTS URL. `tts-end` means that the URL is available, not that audio synthesis or playback is complete.
- A TTS entity can override `async_stream_tts_audio` to stream audio, also when the agent does not stream text.
- The HA `2026.10` agent receives only the complete transcript.
- The `partial` transcript API in PR [184336](https://github.com/home-assistant/core/pull/184336) is on the development branch only in this design snapshot.
- HA closed barge-in PR [182776](https://github.com/home-assistant/core/pull/182776) without a merge.

Upstream source:

- [STT entity and SpeechAudioProcessing](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/stt/__init__.py)
- [Default pipeline](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/assist_pipeline/default_pipeline.py)
- [TTS entity](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/tts/__init__.py)
- [TTS helper](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/tts/helper.py)

The only non-public HA interface in the design is `tts.helper.get_engine_instance`.
A subsequent contract test must examine its behavior and signature on each selected HA version.
The bootstrap does not call it or claim to supply that test.

## Satellite playback

HA `assist_satellite.announce` and `ask_question` cancel an active pipeline before playback.
See [the satellite entity](https://github.com/home-assistant/core/blob/1f214b8461dd1fbbd66e10765c6905f49d3c1d86/homeassistant/components/assist_satellite/entity.py).

ESPHome `voice_assistant` sends `AnnounceFinished` from a 2 s stall timer.
Playback restarts that timer.
If audio does not start in 2 s, HA can receive an incorrect end signal.
See [the ESPHome 2026.10.0 implementation](https://github.com/esphome/esphome/blob/704551a6c93c29a311802f7ee97d67a9e9bce718/esphome/components/voice_assistant/voice_assistant.cpp).
The end of the service call and `idle` must not give the late-result service proof of playback.

## Client release contract

The bootstrap manifest uses `requirements: []` and does not install the unpublished client.
A release manifest must use `assist-live-client==<exact version>` with a numeric `X.Y.Z` version.
Publish that client version to PyPI before the integration release.
The bootstrap checker rejects other requirement names, version ranges, and more entries.

## Prototype results and limits

The prototype used stock HA `2026.10.3` and the built-in conversation agent.
It replayed 10 synthetic commands and 10 synthetic pause commands through the pipeline WebSocket API.
It did not measure satellite playback.

| Prototype measure | Result |
|---|---|
| First TTS audio byte, p50 | Baseline 2,137 ms, prototype 997 ms (53% less) |
| First TTS audio byte, p90 | Baseline 2,192 ms, prototype 1,021 ms |
| Correct transcript and intent | 5 of 10 in each pipeline |
| Synthetic pause cuts | Prototype 10 of 10, baseline 0 of 10 |

These are prototype results, not P0 or P1 results or release performance claims.
The 20M sherpa-onnx streaming model did not keep first words. The design rejected it.

Parakeet stays the local STT default until a streaming model satisfies the replay gate.
The synthetic pause result is not a measurement of human voices.
The release must have a safer turn policy and a human pause test.
No private runtime artifact or recording is part of this repository.
