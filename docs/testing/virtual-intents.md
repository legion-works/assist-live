# Virtual intent tests

The project is experimental and has no release.
These tests use an isolated HA 2026.10.3 instance, not household devices.
They check the text-to-intent path. They do not measure STT precision or device latency.

## Installation

The test container is `assist-live-ha-test` at `http://127.0.0.1:18123`.
Its configuration volume must be `assist-live-ha-test-config`.
The installer refuses a different volume.
The instance must have `input_boolean.test_light` and `input_boolean.test_fan`.
It must also have the conversation, intent, HTTP, API, and WebSocket components.

WARNING: Use only the isolated test instance. Installation changes its configuration and restarts the container.
The installer keeps the initial configuration in `configuration.before-virtual` in the test volume.
It does not delete or replace the container.

Set `ASSIST_LIVE_TEST_TOKEN_FILE` to an external JSON file with a `token` string.
Do not print the token or put the file in Git.

```sh
export ASSIST_LIVE_TEST_TOKEN_FILE="$TEST_HA_TOKEN_FILE"
uv run python tools/test_environment.py --labels tests/data/labels.jsonl --split held_out --install-virtual --check-evidence
```

Installation copies the test agent from `tests/fixtures/custom_components/assist_live_test_agent/` to the test volume.
It does not copy the agent into `custom_components/assist_live/`.
The public config-entry flow makes one agent entry.
A second installation uses the same entry.

`ha-virtual.yaml` supplies `light.virtual_light` and `fan.virtual_fan` through template entities.
The agent adds a virtual timer receiver through the public HA timer-handler interface.
Native HA timers are not `timer` helper entities.
The receiver has a device and an area in the HA registries.
Commands with a time interval use native HA timers and the same virtual light target.
`intents.json` maps each corpus intent to its necessary virtual targets.
An unknown corpus intent stops the run.

The tool hides all entities from Assist, then shows only the virtual light and fan.
Do not use this configuration on a different HA instance.

## Conversation path

`conversation.assist_live_short` and `conversation.assist_live_long` receive usual HA conversation input.
Each agent delegates recognition and execution to the standard HA agent through `conversation.async_converse`.
It gives HA the initial context, device, satellite, and language.
It does not interpret sentences or change HA intent handlers.
Tests check hidden entities, conflicting areas, public service-call events, and commands with time intervals.

The short response has 38 characters and does not stream text.
The long response has more than 60 characters and uses the HA chat-log delta interface.
Tests attach a delta listener to check the actual stream, not only the last text.
The two response strings stay constant for P0 and P1.

The tool sorts replay recording IDs in the selected partition.
The first half uses short replies. The second half uses long replies.
Each group must contain a minimum of 50 commands.
A group with fewer commands stops the run.
The current held-out replay set supplies 100 commands for each group.

The tool sends each labelled transcript through the usual conversation API in the two phase runs.
These runs check constant response words, intent selection, and operation records.
They are not audio replays or P0/P1 acceptance measurements.

## Operation records

The agent sends `assist_live_test_record` public events.
The tool subscribes to these events through the public WebSocket API.
It does not read administrator pipeline-debug APIs.
The agent records:

- The input transcript, selected HA intent, and slots.
- The input context ID and HA response type.
- Public `call_service` events with the same context ID.
- Native timer-handler callbacks, with the event type and timer interval.
- Satisfactory responses to commands with time intervals, with the command and interval.
- The response group, response text, and yielded text deltas.

A scheduled-command record shows satisfactory scheduling, not subsequent execution.
A timer record shows a native `started` callback, not a completed timer.
Only an `action_done` HA response can satisfy the operation check.
Missing fields, empty operation records, and text streams that are not complete cause failure.
The tool compares each timer interval with its corpus `intent` value and compares response records across the two phase runs.
It cancels native timers after each phase run.

No record is part of default runtime transcript storage.
Use `--output "$TEST_RECORDS"` to keep JSONL records external to the repository.
The output contains transcripts. Do not publish it without a data check.

The success lines are:

```text
PASS virtual intent coverage
PASS short and long groups
PASS transcript intent action evidence
```

## Removal

This command removes the test agent entry through the public config-entry API:

```sh
uv run python tools/test_environment.py --labels tests/data/labels.jsonl --split held_out --check-evidence --uninstall-virtual
```

It keeps fixture files and virtual template entities in the isolated volume.
To use the initial configuration again, use the saved configuration and restart only the test container.
