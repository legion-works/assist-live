# Corpus contract

The project is experimental and has no release.
`labels.jsonl` contains 0 human rows at this time.
`synthetic.jsonl` also contains 0 rows.
The committed digest file records this empty metadata state, not an acceptance result.
The P0 baseline and P1 acceptance stay blocked until an approved corpus is available.

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
| `source_uri` | `consent:CONSENT_ID`, or the HTTPS URL for the pinned audio file |
| `source_revision` | Non-empty external bundle or public source revision |
| `audio_sha256` | Lowercase SHA-256 of the complete WAV file |
| `sample_rate` | Positive integer rate in Hz, equal to the WAV rate |
| `last_voice_sample` | Zero-based index of the last voice sample, less than the WAV frame count |
| `transcript` | Correct, non-empty transcript |
| `intent` | Object with a non-empty `name` and a `slots` object |
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
The source URL must supply the correct WAV bytes, not an archive or a dataset landing page.
Do not use a license with an NC or SA condition.

## Fetch and check

Set `CORPUS_AUDIO` to an external audio directory.
For volunteers, import the approved external bundle into that directory first.
`fetch` does not get private consent records or download volunteer audio.
It downloads missing public HTTPS files and checks their hashes before writing them.
It checks files that are available without replacement.
It does not play audio.

```sh
uv run python tools/corpus.py fetch --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO"
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

The empty committed corpus must give named count failures.
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
