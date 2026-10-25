# Corpus contract

The project is experimental and has no release.

`labels.jsonl` contains 250 human timer replay rows from Timers and Such v1.0.
`synthetic.jsonl` also contains 0 rows.
The pause and prefix sets contain 0 rows.
The committed digest file does not pin the imported rows.
Manual boundary review and volunteer collection stay necessary before a freeze.
The P0 baseline and P1 acceptance stay blocked.

## Corpus records

`load_corpus(path: Path) -> list[dict]` reads one JSON object for each non-empty line.
Invalid JSON and non-object rows cause a `ValueError` with a line number.
`validate_corpus(rows: list[dict]) -> list[str]` collects schema, duplicate, speaker, and count failures.
An empty failure list means that these schema checks are satisfactory.
The CLI also checks files, records, and the freeze.

| Field | Contract |
|---|---|
| `id` | Non-empty, unique recording identifier |
| `speaker_id` | Anonymous `speaker-ID` identifier for a human, or `synthetic-ID` for synthetic audio |
| `speaker_type` | Optional `human` or `synthetic`, in agreement with the input file |
| `set` | `replay` or `pause` |
| `split` | `tuning` or `held_out` |
| `audio_path` | Relative WAV path below `--audio-root` |
| `source_uri` | `consent:CONSENT_ID`, a pinned HTTPS WAV URL, or a Zenodo archive-member locator |
| `source_revision` | External bundle revision, public file revision, or archive SHA-256 |
| `audio_sha256` | Lowercase SHA-256 of the complete WAV file |
| `sample_rate` | Integer rate in Hz, equal to the mono WAV rate of 16000 |
| `last_voice_sample` | Zero-based index of the last voice sample, less than the WAV frame count |
| `transcript` | Correct, non-empty transcript |
| `intent` | Object with an HA intent `name` from the pinned name set and a `slots` object |
| `executable_prefix` | Boolean, `true` only for a pause command with an executable prefix |
| `license` | Only `CC0-1.0` or `CC-BY-4.0` |
| `attribution` | Credit text, non-empty for `CC-BY-4.0` |
| `consent_id` | Consent identifier for volunteers, or `null` for a public dataset |

Human identifiers use `speaker-` followed by lowercase letters, digits, or hyphen-separated groups.
Synthetic identifiers use the same structure with the `synthetic-` prefix.
The human input file defaults to `speaker_type=human`.
The synthetic input file defaults to `speaker_type=synthetic`.

Synthetic rows do not supply human counts for the two partitions.
The tests make WAV files with no voice to test the contract.
These files do not supply human voice results.

These fields are also necessary for pause rows:

| Field | Contract |
|---|---|
| `pause_start_sample` | Zero-based start of the internal pause |
| `pause_end_sample` | End-exclusive pause boundary, before `last_voice_sample` |
| `pause_annotation` | Only `manual` |

The pause starts at or after sample 0.
Its end follows its start.
The pause time is `(pause_end_sample - pause_start_sample) / sample_rate`.

It must be 400 to 800 ms, with the two limits included.
A person must mark the two pause boundaries and the last voice sample.

Do not use an ASR guess for `pause_annotation=manual`.
The checker checks sample limits and the manual declaration.
The declaration alone does not show that a person marked the boundaries.
The correct intent must include changes after the pause, such as a five-minute interval.

## Partitions and counts

| Partition | Human replay | Human pause | Executable-prefix pause subset |
|---|---|---|---|
| `held_out` | Minimum 200 | Minimum 300 | Minimum 30 |
| `tuning` | Minimum 50 | Minimum 100 | Minimum 30 |

Each held-out set must contain a minimum of 10 human speakers.
Recording identifiers and audio hashes must be unique across the two partitions and the synthetic input.
Do not tune on held-out data.
Select and freeze 300 held-out pause commands before the experiment.
The input checker checks a minimum of 300.
The experiment size must stay at 300.

Do not add samples after an unsatisfactory experiment.
Report synthetic results independently from human results.

## Sources and consent

Get operator approval for the source before import.
See [Recording consent](recording-consent.md) for volunteer consent and the public index.
Keep signed records and all audio external to Git.
`LICENSES.json` lists the permitted license identifiers in its `licenses` array.
Each row must use an identifier from that list.

The consent index must contain each volunteer consent identifier with the same license.
Public dataset rows pin the HTTPS audio URL, source revision, hash, and necessary credit.
An HTTPS source URL must supply the correct WAV bytes, not a dataset landing page.
An archive source uses `zenodo:RECORD/FILE.zip#MEMBER.wav` and the archive SHA-256 as `source_revision`.
Archive rows use `sha256/AUDIO_SHA256.wav` as `audio_path`.
Do not use a license with an NC or SA condition.

## Timer replay source

The source is **Timers and Such v1.0**, Zenodo record `4662102`, with human voice only.
Do not use v0.1, record `4110812`, which has different license terms.
The version-specific archive URL is:

```text
https://zenodo.org/api/records/4662102/files/timers-and-such-real.zip/content
```

| Pin | Value |
|---|---|
| Archive | `timers-and-such-real.zip` |
| Size | `211480934` bytes |
| SHA-256 | `c92772c830109a6d685b2106fa3f28fc7b1184bc74722045282ed7efb0cf135a` |
| Publisher MD5 | `0aba31a0d0f884a0200b0659490ce0c8` |
| Internal license member | `LICENSE` |
| License heading | `CC0 1.0 Universal` |

`tests/data/LICENSES.json` contains the full internal license text and a source credit line.
CC0 does not make attribution necessary. Each row keeps the source credit anyway.
The consent index stays empty because these rows are not volunteer recordings for this project.

The [collection paper](https://arxiv.org/html/2104.01604v2) gives the consent checkbox text in Appendix A.2.
Its consent text includes this statement:

```text
Only your audio data, and whatever anonymous demographic information you choose to provide, will be stored on a web-based server and be made publicly available for download.
```

This shows collection-level consent, not an individual signed consent record.
Only `test-real` and `train-real` supply the replay rows. The import does not use `dev-real` or synthetic voice.

### Deterministic selection

The CSV members are `test-real.csv` and `train-real.csv` in the pinned archive.
The filter keeps rows with `SetTimer` as the parsed `semantics.intent`.
The source has 80 test timers from 10 session IDs and 545 training timers from 71 session IDs.

The test and training session ID sets do not overlap.

The selection uses Python `random.Random` with seed `4662102`:

1. Sort each source partition by `path`.
2. Keep all 80 test timers in `held_out`.
3. Remove test session IDs from the training session pool.

4. Sort the remaining session IDs, then shuffle them with the seed.
5. Use the first 20 session IDs for the training part of `held_out`.
6. Shuffle their path-sorted timer rows with seed `4662103`, then keep the first 120 rows.

7. Remove all held-out session IDs from the tuning pool.
8. Shuffle the remaining path-sorted training rows with seed `4662104`, then keep the first 50 rows.
9. Sort the selected rows by project partition and source path.

Recording IDs use `tas-v1-` plus the first 20 hexadecimal digits of the source-path SHA-256.
Speaker IDs use `speaker-` plus the first 16 hexadecimal digits of the source-session-ID SHA-256.

The import checks that recording IDs, complete audio hashes, and nominal speaker IDs do not overlap across partitions.
Each selected WAV header reports 16 kHz, mono, and 16-bit PCM.
The import keeps the source WAV bytes without changes.

Source speaker IDs identify sessions, not proven individual persons.
The authors selected test sessions through unique age, gender, and fluency combinations.
That separation is correct only if the reports are truthful. Different session IDs do not prove different persons.

The source CSV transcript is a prompt, not a manually checked transcript of each recording.
Timer-only results do not show precision for other HA domains.

### HA intent mapping

`transcript` comes from the CSV `transcription` column without changes.
`SetTimer` maps to `HassStartTimer`.
The source timer slots map directly to `hours`, `minutes`, and `seconds`, with `0` values unchanged.
The pinned `home-assistant-intents==2026.10.6` package gives these slots in `home_assistant_intents/intents.json`.

Its upstream source is [`intents.yaml`](https://github.com/home-assistant/intents/blob/main/intents.yaml).
`tools/corpus.py` sets the intent-name set to the 42 names in that package.
It rejects source intent names such as `SetTimer`.

### Automatic voice boundaries

Each replay row has `last_voice_annotation: "automatic"`. No row claims manual boundary approval.
The primary method reads the PCM samples and divides their values by 32768.
It calculates RMS energy in 160-sample windows (10 ms), one after the other.
It also uses the last window if that window has fewer than 160 samples.

The threshold is `max(0.005, 0.08 * maximum_window_RMS)`.
The boundary is the last sample in the last window at or above that threshold, but less than the WAV frame count.
This method can include noise or ignore a quiet last consonant. A person must check the boundary before a freeze.

The cross-check uses known-transcript `CTC forced alignment`, not Whisper word timestamps.
The tool is `torchaudio==2.7.1+cpu`, with `torch==2.7.1+cpu`, Python `3.12.13`, and the `MMS_FA` model.
The [official tutorial](https://docs.pytorch.org/audio/2.7.0/tutorials/forced_alignment_for_multilingual_data_tutorial.html) gives the CTC API.
The model weights stay external to Git. No aligner dependency is in the project.

The model [download URL](https://dl.fbaipublicfiles.com/mms/torchaudio/ctc_alignment_mling_uroman/model.pt) supplies `model.pt`.
Its SHA-256 is `20ef12963ab4924bef49ac4fc7f58ad5da2ee43b2c11bc8c853c9b90ecdbc680`.
The external scripts use these commands:

```sh
uv venv --python 3.12 "$ALIGN_VENV"
uv pip install --python "$ALIGN_VENV/bin/python" torch==2.7.1+cpu torchaudio==2.7.1+cpu --index-url https://download.pytorch.org/whl/cpu
uv pip install --python "$ALIGN_VENV/bin/python" numpy==2.3.4 pillow==11.3.0
TORCH_HOME="$CORPUS_CACHE/torch" "$ALIGN_VENV/bin/python" "$ALIGN_SCRIPTS/align_ctc.py" --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --archive "$CORPUS_CACHE/timers-and-such-real.zip" --output-root "$ALIGN_RESULTS" --threads 2
"$ALIGN_VENV/bin/python" "$ALIGN_SCRIPTS/tune_energy.py" --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --alignment-root "$ALIGN_RESULTS" --output "$ENERGY_PARAMETERS"
```

The aligner uses `MMS_FA.get_model(with_star=False).eval()` on CPU, with `threads=2` and deterministic algorithms.
Its settings are `seed=4662102` and `normalize_waveform=True`.
Its tokenizer reads the known CSV `formatted_transcription`, not recognized text.
Normalization changes letters to lowercase and keeps ASCII words with internal apostrophes.
This column spells the numbers as words. The published `transcript` column stays unchanged.

The model supplies emissions to `MMS_FA.get_aligner()` and the model tokenizer.
The sample-to-frame ratio is `WAV_frame_count / emission_frame_count`, approximately 20 ms for each CTC frame.
For each word, the first and last token spans supply its start and end samples.
The end is `round(last_token_end_frame * ratio) - 1`, less than the WAV frame count.

Each word score is the mean token posterior, weighted by token-span length.
The utterance score uses the same weights across words.
The `alignment_words` field keeps each word, its two sample boundaries, its score, and its token-frame count.

The `aligned_end_sample` field is the last word end.
The `energy_minus_aligned_samples` field keeps the signed difference.
The `boundary_difference_samples` field keeps the absolute difference.

The review rule collects all applicable causes:

- `boundary_difference`: absolute difference above 800 samples (50 ms). A 50 ms difference does not set this flag.
- `low_alignment_confidence`: utterance score below `0.5`, minimum word score below `0.1`, or a non-finite score.
- `transcript_mismatch`: a possible prompt difference, with a minimum word score below `0.05`.
- `alignment_failed`: no complete known-transcript CTC result.

`boundary_review_required` is `true` if this cause list is not empty.
The checker recalculates the rule and rejects inconsistent flags or cause lists.
A sufficient score does not prove the spoken transcript. Low scores can also show an accent or a quiet word.
The `match` status means model agreement, not manual approval.

The energy calibration used only the 50 tuning recordings.
Their median energy-minus-aligned difference was `3.3125 ms`, below the systematic-bias threshold of `10 ms`.
The energy parameters stay unchanged. No held-out value selected the parameters.
Filtering calibration on intermediate-word confidence changed the selected recordings, so that calibration was not used.

The signed difference statistics are:

| Group | Median ms | p10 ms | p90 ms | Maximum ms | Maximum absolute ms |
|---|---:|---:|---:|---:|---:|
| All 250 recordings | -77.53125 | -217.90625 | 69.15 | 2128.375 | 2128.375 |
| Tuning only | 3.3125 | -210.2875 | 93.8375 | 529.4375 | 1899.3125 |
| Held-out only | -87.0625 | -217.90625 | 63.11875 | 2128.375 | 2128.375 |

Percentiles use linear interpolation at `(count - 1) * percentile`.
The held-out median is different from the tuning median. The rule does not use a correction fitted to held-out data.
An English `WAV2VEC2_ASR_BASE_960H` CTC alternative gave 46 flags on tuning, compared with 39 for `MMS_FA`.
That alternative did not replace `MMS_FA`.

The external review sheet contains one PNG for each flagged row and a fixed-seed sample of 20 rows.
The random sample uses seed `4662105` and the selected row order. Some random rows can also have a flag.
Each PNG shows the waveform, transcript, red energy marker, blue aligned-end marker, and the aligned word boundaries.
Word spans also show their confidence scores.

The index records the two markers, the difference, the selection cause, and a pending review state.
Check these files before a correction or freeze. Keep the sheet and all audio external to Git.

The CTC rule flags 190 recordings and makes 195 PNGs.
Fifteen of the 20 random recordings also have a flag.
There are 178 boundary differences, 64 low-confidence recordings, and 59 possible transcript differences. These groups overlap.

This does not decrease review work compared with the 181 Whisper flags.
A decision about the endpoint method is necessary before acceptance.
The held-out rows have 30 nominal sessions. The tuning rows have 33 other nominal sessions.

## Fetch and check

Set `CORPUS_AUDIO` to an external audio directory.
For volunteers, import the approved external bundle into that directory first.
`fetch` does not get private consent records or download volunteer audio.
It downloads missing public HTTPS files and checks their hashes before writing them.

For archive members, set `CORPUS_CACHE` to an external cache directory, or supply `fetch --cache`.
The default cache is `$HOME/.cache/assist-live/corpus`.
The cache must be external to all Git repositories, also through symlinks.
`fetch` downloads each missing archive one time and names it with its SHA-256.
It checks cached archive bytes before use, reads only the named member, and checks its SHA-256 before writing audio.
It does not write archive paths into the filesystem.

It checks files that are available without replacement.
It does not play audio.

```sh
uv run python tools/corpus.py fetch --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO"
uv run python tools/corpus.py check --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --synthetic tests/data/synthetic.jsonl --licenses tests/data/LICENSES.json --consent tests/data/consent-index.json
uv run python tools/corpus.py check --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --synthetic tests/data/synthetic.jsonl --licenses tests/data/LICENSES.json --consent tests/data/consent-index.json --freeze tests/data/corpus.sha256
uv run python tools/corpus.py check --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --synthetic tests/data/synthetic.jsonl --licenses tests/data/LICENSES.json --consent tests/data/consent-index.json --verify-freeze tests/data/corpus.sha256
```

Run the check with `--verify-freeze` before playback.
Each resolved audio file must agree with its SHA-256.
The checker rejects absolute paths, parent traversal, and Windows volume paths.
It rejects symlinks with a target external to the audio root.
It also checks WAV rates, sample limits, and truncated sample data.
Do not change the files between the check and playback.

`--freeze` writes a manifest only after all checks give satisfactory results.
`--verify-freeze` compares the manifest without changes to it, also on failure.
The manifest includes all input bytes: human records, partition assignments, synthetic records, license records, and consent records.
It also includes audio locators with hashes.
It contains no absolute audio paths.
If an input changes, make a new approved experiment freeze before use.

A correct human corpus gives these lines:

```text
PASS held_out replay>=200 pause>=300 prefix>=30 speakers>=10
PASS tuning counts
PASS frozen locators
```

The replay-only corpus gives replay PASS lines and named pause and prefix count failures.
`--freeze` refuses to write a manifest while these failures stay.
Unit tests build a complete temporary corpus to test the success path.
These tests do not remove the external corpus-supply blocker.

## Pause arithmetic

Use the two-sided Clopper-Pearson 95% confidence interval for the pause-cut rate.
For `k` cuts in `n` samples, the maximum confidence limit is the solution of this equation in `p`:

```text
sum(comb(n, j) * p**j * (1-p)**(n-j), j=0..k) = 0.025
```

The minimum confidence limit is 0 for `k=0`.
For `k>0`, use the same equation through `k-1` with a right side of `0.975`.
The maximum confidence limit is 1 for `k=n`.
The implementation uses standard-library binomial arithmetic and bisection, with no SciPy dependency.
The [Clopper-Pearson interval equations](https://doi.org/10.1214/14-EJS909) give these equal-tailed limits (accessed 2026-10-24).

| Samples | Cuts | Observed rate | Upper bound |
|---|---|---|---|
| 100 | 3 | 3% | 8.5176053% |
| 300 | 9 | 3% | 5.6183548% |

At 100 samples, only 0 or 1 cut satisfies the 3% rate limit and the 6% maximum confidence limit.
At 300 samples, 9 cuts satisfy the two limits.
The executable-prefix subset must have 0 cuts with `profile=safe`.
The tests calculate the confidence limits and the 100-sample acceptance set.
Set the sample count at 300 before the experiment, not after a failure.
