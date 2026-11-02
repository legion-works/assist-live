# Fault runner records

The fault runner uses only the isolated test environment.
Synthetic tests do not prove hardware acceptance.
Get approval before a live bench run.

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

For `speech` and `pause`, the target stop time is the window start plus 25% of its length.
The planned request is earlier by the measured latency.
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
