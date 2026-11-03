# Fault runner records

The fault runner uses only the isolated test environment.
Synthetic tests do not prove hardware acceptance.
Get approval before a live bench run.

## Same-engine baselines

Before each fault, the runner collects a no-fault transcript for each complete recording and STT engine.
The server baseline uses the same pipeline and virtual device as the fault trials.
With `--temporary-agent-pipeline`, this is the same temporary pipeline, not its source pipeline.
The baseline run can operate the virtual targets through the test conversation agent.

The runner finds the assist-live config entry through the pipeline STT entity registry row.
It reads `fallback_stt` from the initial public `options/flow` response and deletes the flow in `finally` without a submission.
It sends the complete PCM recording to the selected fallback entity through public `POST /api/stt/{entity}`.
The request uses `X-Speech-Content` for mono PCM16 at 16000 Hz and the pipeline STT language.
It does not send a WAV file header or use the assist-live fallback path for this baseline.

In HA `2026.10.1` and `2026.10.4`, `homeassistant/components/stt/__init__.py:262-312` declares this authenticated HTTP interface and accepts entity IDs.
Lines `380-422` read its audio metadata header.

Each trial keeps `stt_engines` and `baselines`, with the engine entity ID, `clip_id`, audio SHA-256, transcript, and baseline status.
The server baseline also keeps its pipeline ID and no-fault run records.
The runner collects each recording one time and uses its baseline records for subsequent trials in that run.

The diagnostic turn row selects the transcript engine: an STT fallback cause selects `fallback`, and no STT fallback cause selects `server`.
A TTS fallback cause does not select the STT engine.
The classifier compares the trial transcript with that engine's baseline through `tools/result_text.py:words`.
The prompt text does not select fault acceptance.
Intent and device operations must agree with the corpus intent.

An empty HA result with complete records and no device operation stays satisfactory when its baseline is available.
A missing or unsatisfactory baseline gives `status: unavailable`, also for an empty result.
A different engine identity, `clip_id`, or audio SHA-256 also makes the baseline unavailable.

## Recorded causes

Each trial downloads public diagnostics for the pipeline STT config entry before and after the turn.
The runner keeps the `fallback_reasons` counts and their delta.
It records `server_container_stopped` in `injected_fault` and the integration cause in `failure_cause`.

The cause comes from the diagnostic turn row with the trial's `turn_id`, and only from its `stt_fallback` destination.
The row stays in `fallback_turns` in the trial output.
The aggregate delta must agree with that STT cause, or show no change when the turn has no STT fallback.
A missing or duplicate turn row, a TTS-only cause, or a different aggregate delta cannot prove the necessary STT fallback.
Missing diagnostics, invalid counts, and a counter decrease give an unsatisfactory result.

| Stage | Necessary delta |
| --- | --- |
| `before` | One fallback only: `health_timeout`, `health_error`, `server_unhealthy`, or `open_error` |
| `speech`, `pause` | One fallback only: `stream_closed`, `message_timeout`, or `stream_error` |
| `after-final` | No fallback |

The aggregate counts belong to the full config entry.
Keep other turns not active during each trial.
An unrelated fallback can cause an unsatisfactory result.

## Deadline and stop timing

The deadline is 20 s after the HA `run-start` event, not after the fault request.
The CLI has no deadline override.
The classifier checks the recorded deadline against the turn start.

WARNING: The latency measurement stops the isolated server one time before the trials.
The runner measures `docker stop --time 0` with a monotonic clock and starts the server again.
The measurement does not include server restoration time.
Each row keeps this measured latency.

For `speech`, let `L` be the measured stop latency in samples, rounded up.
The stop margin `M` is the larger of `L` and one 20 ms audio frame (320 samples).
The target stop is the window midpoint.
The request is earlier by `L`, but stays a minimum of one frame after the window start.
The predicted stop stays a minimum of `M` plus one frame before the window end.

The runner moves the request into these limits when necessary.
If the window cannot contain these margins, the trial is `unavailable` and starts no fault.

For `pause`, the target stop time stays at the window start plus 25% of its length.
The planned pause request is earlier by the measured latency.
Audio frames limit request timing to 20 ms steps.
The runner records the actual stop sample and rejects a stop that ends at a sample not in the labelled window.
A changed stop latency can cause an unsatisfactory trial.

## Append-only records

Run `task records-check` to check `docs/verification.md` against the merge base of `main` and `HEAD`.
`task check` includes this check.
The check includes uncommitted file changes.
It names each changed, deleted, or new line before the end of the previous record.
Add new records only at the end of the file.

CI fetches the full history.
On a push, the check uses the previous `main` commit from the push event.
On a pull request, it uses the merge base of `origin/main` and `HEAD`.
An unreadable base commit causes failure.
To select a base commit, use `uv run --locked python tools/check_records.py --base "$BASE_COMMIT"`.
