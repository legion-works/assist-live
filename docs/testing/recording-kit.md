# Volunteer recording kit

The project is experimental and has no release.
This kit collects generic commands for virtual test entities, not recordings from a home.
Get operator approval for collection and the source before recording starts.
Keep all audio, signed consent, and identity mappings external to Git.
See [Recording consent](recording-consent.md) and [Corpus contract](corpus.md).

## Written consent

Give the volunteer this text before recording.
Keep the completed consent with the private identity mapping, not in the public records.
The private record must identify the volunteer and the applicable recordings.
Use an anonymous speaker code and consent code in the bundle.

### Consent declaration

This project records generic voice commands for software tests.
The permitted uses are recording, test playback, analysis, and public redistribution.
The recordings and anonymous records can become public test data.
Do not include names, addresses, account information, or other personal data in a recording.

Select one recording license:

- [ ] `CC0-1.0`: dedicate the recording to the public domain with CC0.
- [ ] `CC-BY-4.0`: use the Creative Commons Attribution 4.0 International license for the recording.

For `CC-BY-4.0`, agree on anonymous public credit before recording.
These licenses let other users copy and redistribute the recording.
A withdrawal request cannot remove copies that other users hold.
Before the corpus freeze, the maintainer will remove the recording from the proposed corpus on request.
After freeze or publication, removal of all copies is not possible.

- [ ] The volunteer gives written consent to recording and these permitted uses.
- Anonymous speaker code: `speaker-____`
- Anonymous consent code: `consent-____`
- Selected license: `____`
- Anonymous public credit, if necessary: `____`
- Private signature and date: complete only in the private consent record.

### License terms

Read the full selected terms before consent:
[CC0](https://creativecommons.org/publicdomain/zero/1.0/legalcode.en) or
[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/legalcode.en).
Do not promise withdrawal of rights that an open license grants.
Get legal advice if consent or the license terms are unclear.

## Collection counts and partitions

The pause corpus must contain 100 tuning commands and 300 held-out commands.
Each partition must contain a minimum of 30 executable-prefix commands.
The held-out pause set must contain a minimum of 10 human speakers.
The lists below supply these counts with different speaker groups:

| Group | Speakers | Prompts per speaker | Pause rows | Prefix rows |
|---|---:|---:|---:|---:|
| Tuning | 10 | T01 to T10 | 100 | 30 |
| Held-out | 10 other speakers | H01 to H30 | 300 | 30 |

Select one partition for each speaker before recording.
Do not move a speaker or recording between partitions after calibration.
Each person records each selected prompt one time with satisfactory audio.
If a recording is incorrect, replace it before selection. Do not count second recordings as different commands.

The prefix rows are part of the pause counts, not more rows.
The replay corpus independently must contain 200 held-out commands and 50 tuning commands.
The timer source supplies those replay rows. This kit does not replace them.

## Recording procedure

1. Get written consent before recording.
2. Use a quiet room without other voices, music, or television.
3. Put the microphone about 20 cm from the mouth and a small distance to one side.
4. Keep the device position unchanged for all recordings from that speaker.
5. Use the usual speaking level. Do not whisper or shout.
6. Record one prompt for each file, with about one second without voice before and after the command.
7. At `[pause]`, make one natural pause of 400 to 800 ms.
8. Do not speak the word "pause" or add digital audio with no voice.
9. Keep the words on the two sides in one command, without a new wake word.
10. Listen to the recording. Replace clipped or incorrect recordings, or recordings with interruptions.
11. Use 16 kHz mono, 16-bit PCM WAV without noise reduction or time changes.
12. If conversion is necessary, use the command below before the last hash and boundary marks.
13. Name each file with the anonymous speaker code and prompt code.

Example file name: `speaker-01-t01.wav`.
Use different anonymous codes for different speakers.
Do not put a name, device name, location, or date of birth in a file name or WAV metadata.

```sh
ffmpeg -i input.wav -map_metadata -1 -ac 1 -ar 16000 -c:a pcm_s16le speaker-01-t01.wav
```

The conversion tool is optional and external to the corpus checker.
Keep only the last recording after conversion in the proposed bundle.
A person must mark the internal pause boundaries and last voice sample that they can hear.
The pause time must satisfy the 400 to 800 ms limits after conversion.
Do not decrease a pause time to make a recording give satisfactory results.

## Tuning prompts

T01 to T03 have executable prefixes.
Their words before `[pause]` make a complete command.
Their words after `[pause]` change that command with a time condition.
The test light, test fan, and test switch are virtual entities only.

| Code | Spoken command |
|---|---|
| T01 | `Turn off the test light [pause] in five minutes.` |
| T02 | `Turn on the test fan [pause] in two minutes.` |
| T03 | `Turn off the test switch [pause] in ten minutes.` |
| T04 | Set a timer for [pause] five minutes. |
| T05 | Set a timer for [pause] two minutes and ten seconds. |
| T06 | `Turn on [pause] the test light.` |
| T07 | `Turn off [pause] the test fan.` |
| T08 | Set the test light brightness to [pause] fifty percent. |
| T09 | Set the test speaker volume to [pause] twenty percent. |
| T10 | Set the test thermostat temperature to [pause] twenty degrees. |

## Held-out prompts

H01 to H03 have executable prefixes with time conditions.
Keep these recordings external to calibration and method selection.

| Code | Spoken command |
|---|---|
| H01 | `Turn on the test light [pause] in three minutes.` |
| H02 | `Turn off the test fan [pause] in four minutes.` |
| H03 | `Turn on the test switch [pause] in six minutes.` |
| H04 | Set a timer for [pause] one minute. |
| H05 | Set a timer for [pause] three minutes. |
| H06 | Set a timer for [pause] seven minutes. |
| H07 | Set a timer for [pause] ten minutes. |
| H08 | Set a timer for [pause] thirty seconds. |
| H09 | Set a timer for [pause] forty seconds. |
| H10 | Set a timer for [pause] one minute and five seconds. |
| H11 | Set a timer for [pause] two minutes and twenty seconds. |
| H12 | Set a timer for [pause] four minutes and thirty seconds. |
| H13 | Set a timer for [pause] six minutes and ten seconds. |
| H14 | Set a timer for [pause] eight minutes and fifteen seconds. |
| H15 | Set a timer for [pause] one hour. |
| H16 | `Turn on [pause] the test fan.` |
| H17 | `Turn off [pause] the test light.` |
| H18 | `Turn on [pause] the test switch.` |
| H19 | `Turn off [pause] the test switch.` |
| H20 | Set the test light brightness to [pause] ten percent. |
| H21 | Set the test light brightness to [pause] thirty percent. |
| H22 | Set the test light brightness to [pause] seventy percent. |
| H23 | Set the test light brightness to [pause] ninety percent. |
| H24 | Set the test speaker volume to [pause] ten percent. |
| H25 | Set the test speaker volume to [pause] thirty percent. |
| H26 | Set the test speaker volume to [pause] forty percent. |
| H27 | Set the test thermostat temperature to [pause] eighteen degrees. |
| H28 | Set the test thermostat temperature to [pause] nineteen degrees. |
| H29 | Set the test thermostat temperature to [pause] twenty-one degrees. |
| H30 | Set the test thermostat temperature to [pause] twenty-two degrees. |

The prompt is not proof of the spoken transcript or intent.
Check the actual words and the complete intended command, with the time condition.
Do not remove the time condition from intent slots on a prefix row.
Use the pinned HA intent names and the slot contract for the virtual test entities.

## Bundle and corpus entry

Send these items through the approved private connection:

- The last WAV files with anonymous file names.
- A record file with the correct spoken transcripts, prompt codes, and selected partition.
- Manual pause boundaries and last voice sample indexes after conversion.
- The anonymous speaker code, consent code, selected license, and agreed public credit.
- A bundle revision and the location of the private written consent.

Keep signatures, identities, and contact information in the different private consent record.
Before freeze, route a withdrawal through the consent code to the maintainer.
Remove that audio and the proposed records from the collection.
Do not publish a withdrawal request or identity mapping.

The consent-bundle source does not download or transform volunteer files.
The maintainer checks consent, personal data, audio, transcripts, and intent slots before import.
Copy approved WAV files below an external `CORPUS_AUDIO` directory.
Use their relative paths as `audio_path` and calculate SHA-256 after conversion.
For each record, set these fields:

- `source_uri: "consent:consent-CODE"` and the same `consent_id`.
- `source_revision`: the pinned external bundle revision.
- `license`: the volunteer's selected `CC0-1.0` or `CC-BY-4.0`.
- `attribution`: the agreed anonymous credit, necessary for `CC-BY-4.0`.
- `set: "pause"`, selected `split`, and anonymous `speaker_id`.
- `sample_rate: 16000`, `audio_sha256`, correct transcript, and complete HA intent slots.
- `pause_start_sample`, end-exclusive `pause_end_sample`, and `pause_annotation: "manual"`.
- `last_voice_sample`, `last_voice_annotation: "manual"`, and the anonymous annotator note.
- `executable_prefix: true` only for T01 to T03 or H01 to H03 after a satisfactory inspection of the spoken words.

Use unique recording IDs and audio hashes.
Add the four-field redacted consent record to `tests/data/consent-index.json` as specified in [Recording consent](recording-consent.md).
The license identifier must also be in `tests/data/LICENSES.json`.
Run the checker before an approved freeze:

```sh
uv run --locked python tools/corpus.py check --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --synthetic tests/data/synthetic.jsonl --licenses tests/data/LICENSES.json --consent tests/data/consent-index.json
```

The `fetch` command reports `external consent bundle required` if approved volunteer audio is missing.
Do not replace that failure with a public download URL for private files.
No volunteer recording or corpus freeze is complete at this time.
