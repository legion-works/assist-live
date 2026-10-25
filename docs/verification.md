# Verification record

## Package metadata

Date: 2026-10-24 (UTC).
The source is [PyPI metadata for HA 2026.10.3](https://pypi.org/pypi/homeassistant/2026.10.3/json), field `info.requires_python`.
The fetched value is `>=3.14.2`.
The project uses `requires-python = ">=3.14.2"` and `.python-version` value `3.14.2`.
No HA or unpublished client package is a bootstrap dependency.

The [HACS manifest documentation](https://www.hacs.xyz/docs/publish/start/#hacsjson) lists the `name` and `homeassistant` keys.
It does not list `render_readme`, so this bootstrap does not include that key.
The package is not in the repository root, so it does not set `content_in_root`.

## Package boundary tests

Date: 2026-10-24 (UTC).
The standalone command ran before the project had metadata:

```sh
cd "$WORKSPACE/assist-live"
uv run --no-project --with pytest pytest tests/test_package.py -q
```

Result:

```text
E       FileNotFoundError: [Errno 2] No such file or directory: 'custom_components/assist_live/manifest.json'
1 failed in 0.04s
```

The public-tree tests also ran before the checker was available.
Result: `29 failed in 0.41s`, with missing checker and `ModuleNotFoundError: No module named 'tools'` failures.

## Git boundary

Date: 2026-10-24 (UTC).
An ancestor check ran before `git init -b main`.
Result: `PASS no Git ancestor`.
The repository-root check ran after initialization:

```sh
test "$(git rev-parse --show-toplevel)" = "$(pwd -P)" && printf 'PASS own Git root\n'
```

Result: `PASS own Git root`.
At initialization, the repository had 0 commits and no remote. No publication occurred.

## Bootstrap validation

Date: 2026-10-24 (UTC).
The mandatory bootstrap command ran from `$WORKSPACE/assist-live`:

```sh
uv sync --locked && uv run pytest tests/test_package.py -q && uv run python tools/check_bootstrap.py
```

Result lines:

```text
Resolved 7 packages in 1ms
Checked 5 packages in 0.13ms
1 passed in 0.01s
PASS integration bootstrap
```

The checker tests ran after the package check:

```sh
uv run pytest tests/test_public_tree.py -q
uv run pytest tests/test_package.py tests/test_public_tree.py -q
```

Result lines:

```text
35 passed in 1.42s
36 passed in 1.39s
```

The format and lint gates ran from the same root:

```sh
uvx ruff format --check .
uvx ruff check .
```

Result lines:

```text
18 files already formatted
All checks passed!
```

The STE checker ran on each of the 12 Markdown files in the repository.
Each file has remaining finding count `0` and exit code `0`.
The command for each file was:

```sh
python3 "$STE_CHECK" "$FILE"
```

Here, `STE_CHECK` is the external ASD-STE100 checker.

The public-tree gate ran with the operator's external denylist:

```sh
uv run python tools/check_public_tree.py --history --root . --private-denylist "$PRIVATE_DENYLIST"
```

Result lines:

```text
PASS own Git root
PASS history secrets commits=0
PASS public data matches=0
```

A different check used `git ls-files -co --exclude-standard` and case-insensitive literal searches for each denylist entry.
It also examined private path prefixes and RFC 1918 addresses.
Result lines:

```text
files=28 denylist=0 private_path=0 rfc1918=0
PASS independent publishable-file self-scan
```

A temporary Git fixture used one actual external-denylist entry in a temporary file.
The checker accepted the baseline, rejected the mutation with `denylist=1`, and accepted the fixture after file removal.
The output contained only counts and object identifiers.
The fixture did not go into this repository.

```text
PASS mutation baseline exit=0
PASS mutation rejected exit=1
FAIL public data objects=1 matches=1
PASS mutation removal exit=0
```

## Static analysis baseline

Date: 2026-10-24 (UTC).
A static analysis snapshot examines the repository root.
Its snapshot was `FRESH`, with authoritative diagnostics for 6 of 6 Python files.
Its first full report had 4 errors and 0 warnings:

```text
config_flow.py:3: Import "homeassistant" could not be resolved [Pyright]
config_flow.py:4: Import "homeassistant.data_entry_flow" could not be resolved [Pyright]
config_flow.py:7: Incorrect keyword arguments for __init_subclass__ method [Pyright]
config_flow.py:7: No parameter named "domain" [Pyright]
```

The project environment intentionally contains pytest only, not HA.
CI must add the HA type environment before these diagnostics can give a clean result.
This is `not a passing type check`.

After the stock HA correction below, the last snapshot has 3 errors and 0 warnings:

```text
config_flow.py:3: Import "homeassistant" could not be resolved [Pyright]
config_flow.py:6: Incorrect keyword arguments for __init_subclass__ method [Pyright]
config_flow.py:6: No parameter named "domain" [Pyright]
```

## Stock HA import and type checks

Date: 2026-10-24 (UTC).
Temporary HA and mypy environments check the package without project dependency changes.
The import command was:

```sh
UV_LINK_MODE=copy uv run --no-project --python 3.14 --with homeassistant==2026.10.3 python -c "import custom_components.assist_live, custom_components.assist_live.config_flow as f; print('import ok', f.__name__)"
```

Result: `import ok custom_components.assist_live.config_flow`.

The first mypy command used `--ignore-missing-imports=False`.
The tool rejected that flag:

```text
mypy: error: argument --ignore-missing-imports: ignored explicit argument 'False'
```

The corrected command keeps missing-import checks active by default:

```sh
UV_LINK_MODE=copy uv run --no-project --python 3.14 --with homeassistant==2026.10.3 --with mypy mypy custom_components/assist_live
```

The first check found two errors in the config flow type.
HA uses `config_entries.ConfigFlowResult`, not `data_entry_flow.FlowResult`, for `async_step_user`.
The method uses the HA config-entry type.
After that correction, the two commands ran again with these results:

```text
import ok custom_components.assist_live.config_flow
Success: no issues found in 2 source files
```

The stock HA check does not replace integrated type tooling and HA runtime tests.

## Public-tree checker contract

The checker uses the publishable worktree list from `git ls-files -co --exclude-standard`.
It does not include ignored untracked files. It always examines indexed files, also when ignore rules name them.
With `--history`, it also examines each blob and path in each reachable commit and all commit metadata.
It rejects symlinks and submodule entries rather than follow data external to the checked tree.

The denylist is external to Git. Output contains counts and object IDs only.
The checker rejects private address ranges, ULA addresses, private user paths, unlicensed audio, and unknown binary data.
It accepts documentation-range IPv4 addresses and loopback examples.
Audio must have an explicit approved SHA-256 hash, also if its bytes decode as text.

The checker always calls `gitleaks git` for `--history`, also when there are no commits.
A missing tool or a nonzero scanner exit stops the gate.
No commits gives an explicit `commits=0` result, not a claim that history contains examined commits.
Shallow history stops the gate because it cannot prove a complete history check.
The integration extends the default Gitleaks rules and has no exemptions.

## Deferred checks

CI includes the HA test harness, strict type checks, and mandatory HACS validation.
Local HACS validation cannot run without GitHub credentials and a published repository.
Stock hassfest validation below uses no project dependency changes.
The bootstrap config flow aborts and makes no config entry.
No runtime, hardware, cloud, release, or Marketplace acceptance result follows from these checks.

## Metadata and privacy validation

Date: 2026-10-24 (UTC).
Validation examines manifest order, config steps, IPv4 punctuation, private paths, and nested-repository detection.
The package is experimental and aborts setup.

The manifest uses `domain`, `name`, then alphabetical keys.
The English strings contain a `user` step with a title and description that state the bootstrap limit.
The bootstrap checker checks that order and those strings.
A named step documents the `async_step_user` entry point more clearly than an empty `step` object.
It does not make setup available.

The IPv4 check accepts a period after an address but not a continuation with a digit group.
Version strings `2026.10.3` and `1.10.0.0` have no finding.
The path check finds user paths with no last separator.
The Git-root check rejects a root with a `.git` entry in a parent directory.

### Regression results

The selected tests ran before production changes:

```sh
uv run pytest tests/test_public_tree.py -q --tb=short -k 'hassfest_metadata or before_period or versions_and_digit or without_trailing_separator or inside_another_repository'
```

RED lines, with synthetic address and path values not shown:

```text
test_shipped_hassfest_metadata_shape: At index 5 diff: 'issue_tracker' != 'integration_type'
test_bootstrap_rejects_hassfest_metadata_regressions: assert 'manifest key order' in []
test_bootstrap_rejects_hassfest_metadata_regressions: assert 'config flow steps' in []
test_private_ipv4_before_period_is_rejected: assert 0 == 1
test_bootstrap_rejects_private_address_before_period: assert 'public data README.md' in []
test_private_path_without_trailing_separator_is_rejected: assert 0 == 1
test_repository_inside_another_repository_is_rejected: Failed: DID NOT RAISE ValueError
21 failed, 4 passed, 35 deselected in 1.80s
```

The same command after the repairs gave:

```text
25 passed, 35 deselected in 1.68s
```

The complete mandatory command sequence ran from `$WORKSPACE/assist-live`:

```sh
uv sync --locked && uv run pytest tests/test_package.py -q && uv run python tools/check_bootstrap.py
uv run pytest tests/test_public_tree.py -q
uv run pytest tests/test_package.py tests/test_public_tree.py -q
uv run python tools/check_public_tree.py --history --root . --private-denylist "$PRIVATE_DENYLIST"
uvx ruff format --check .
uvx ruff check .
```

Result lines:

```text
Resolved 7 packages in 0.99ms
Checked 5 packages in 0.08ms
1 passed in 0.00s
PASS integration bootstrap
60 passed in 2.38s
61 passed in 2.21s
PASS own Git root
PASS history secrets commits=0
PASS public data matches=0
18 files already formatted
All checks passed!
```

### Hassfest validation

The validator source came from the `home-assistant/core` tag `2026.10.3` archive.
`HASSFEST_ROOT` below is the temporary directory with that tag's `script` package.
The method uses the stock validator and stock HA in a temporary environment.
It does not skip plugins or change validator source.

The first run stopped on an upstream import:

```text
ModuleNotFoundError: No module named 'infrared_protocols'
```

The stock infrared manifest pins `infrared-protocols==10.1.0`.
That package satisfies the validator's device-class import.
The last command was:

```sh
cd "$HASSFEST_ROOT"
UV_LINK_MODE=copy uv run --no-project --python 3.14 --with homeassistant==2026.10.3 --with infrared-protocols==10.1.0 --with pipdeptree==4.2.5 --with tqdm==4.70.1 --with ruff python -m script.hassfest --action validate --integration-path "$WORKSPACE/assist-live/custom_components/assist_live"
```

Result:

```text
Integrations: 1
Invalid integrations: 0
```

All 23 integration plugins ran. The empty `codeowners` list is correct for this hassfest schema.
HACS validation is a different future check.
No HA, validator, or mypy dependency went into `pyproject.toml` or `uv.lock`.

### Privacy and static analysis checks

The different publishable-file check used literal denylist searches and a different address-check implementation.
It gave:

```text
PASS own Git root
files=28 denylist=0 private_path=0 rfc1918=0
PASS independent publishable-file self-scan
```

A static analysis snapshot is `FRESH`, with authoritative reports for 6 of 6 Python files.
It has the same 3 HA-environment errors and 0 warnings recorded above.
The HA type environment is not part of bootstrap. This is `not a passing type check`.

The ASD-STE100 checker ran on each of the 12 public Markdown files.
Each file has 0 findings and exit code 0.
The command for each file was `python3 "$STE_CHECK" "$FILE"`, as specified above.

## HA minimum and portable package tests

Date: 2026-10-24 (UTC).
The Unix-path expression rejects local user paths but accepts the two public URL test inputs.
The four no-trailing-separator tests continue to reject private paths.

The project selects HA `2026.10.1` as the HACS minimum for the planned version matrix.
The bootstrap checker rejects the former `2026.10.0` minimum.
The design marks the HA minimum, `integration_type`, and `iot_class` as project decisions, not frozen-spec requirements.
The package test resolves its root from `Path(__file__).resolve().parents[1]` and checks the package boundary.

### Test-first results

The two URL tests and the minimum-version mutation ran before production changes:

```sh
uv run pytest tests/test_public_tree.py -q --tb=short -k 'public_urls_are_not_private_user_paths or untested_ha_minimum'
```

RED result lines:

```text
test_public_urls_are_not_private_user_paths: AssertionError: assert not Counter({'private_path': 1})
test_bootstrap_rejects_untested_ha_minimum: assert 'HACS metadata' in ['public data tests/test_public_tree.py']
3 failed, 60 deselected in 0.06s
```

The subdirectory command also ran before the path correction:

```sh
cd "$WORKSPACE/assist-live/tests"
uv run pytest test_package.py -q --tb=short
```

```text
E   FileNotFoundError: [Errno 2] No such file or directory: 'custom_components/assist_live/manifest.json'
1 failed in 0.02s
```

### Package validation

From `$WORKSPACE/assist-live`:

```sh
uv run pytest tests/test_package.py tests/test_public_tree.py -q
uv run python tools/check_bootstrap.py
uv run python tools/check_public_tree.py --history --root . --private-denylist "$PRIVATE_DENYLIST"
uvx ruff format --check .
uvx ruff check .
python3 "$STE_CHECK" docs/design.md
```

Result lines:

```text
64 passed in 1.53s
PASS integration bootstrap
PASS own Git root
PASS history secrets commits=0
PASS public data matches=0
18 files already formatted
All checks passed!
```

The design STE check has 0 findings and exit code 0.
From `$WORKSPACE/assist-live/tests`:

```sh
uv run pytest test_package.py -q
```

```text
1 passed in 0.00s
```

A static analysis result is `FRESH` and has the same 3 HA-environment errors and 0 warnings recorded above.
The HA type environment is not part of bootstrap. This is `not a passing type check`.

## Checker maintenance

The bootstrap checker uses constant HACS metadata, manifest fields, and Python minimum values.
CI must update the checker and its tests in the same change when it changes those values.

## Review of all three repositories

Date: 2026-10-24 (UTC).

The review examined the `assist-live`, `assist-live-server`, and `assist-live-esphome` bootstrap repositories.
It checked cross-repository contracts, convention drift, comment and prose hygiene, and privacy across Git history.
Result: the verification records held process notes, and the server documents did not follow the shared conventions.
This record uses public project state and topic headings.
The checker maintenance rule names the metadata constraints.
The repository's document set and runtime code did not change.

## Current bootstrap checks

Date: 2026-10-24 (UTC).
Commands from `$WORKSPACE/assist-live`:

```sh
uv sync --locked && uv run pytest tests/test_package.py -q && uv run python tools/check_bootstrap.py
uv run pytest tests/test_public_tree.py -q
uv run pytest tests/test_package.py tests/test_public_tree.py -q
uvx ruff format --check .
uvx ruff check .
uv run --project "$WORKSPACE/assist-live" python "$WORKSPACE/assist-live/tools/check_public_tree.py" --history --root . --private-denylist "$PRIVATE_DENYLIST"
```

Result lines:

```text
Resolved 7 packages in 1ms
Checked 5 packages in 0.06ms
1 passed in 0.00s
PASS integration bootstrap
63 passed in 1.42s
64 passed in 1.43s
18 files already formatted
All checks passed!
PASS own Git root
PASS history secrets commits=1
PASS public data matches=0
```

The earlier `commits=0` results are the empty-history baseline at initialization.
The current history check examines the bootstrap commit also.

## CI and local checks

Date: 2026-10-24 (UTC).
The project is experimental and has no release.
The single mandatory GitHub check is `ci-complete`.
Repository administrators must select this check in branch protection after publication.
Its dependencies are `lint`, `typecheck`, `unit`, `ha`, `hassfest`, `hacs`, and `secrets`.
It runs with `if: always()` and rejects each result other than `success`.

Each HA matrix member must supply a `success` output.
A missing or skipped member gives a failure, regardless of the matrix `job` result.
The GitHub runner ignores empty `job` outputs, so each member supplies only its own output.
Source: `actions/runner`, `src/Runner.Worker/JobExtension.cs`, `job` output analysis.

The HA matrix runs all of `tests/` on the two selected versions.
The TTS lookup and voice-option contract files are not in the repository at this time.
The same matrix will run them when the runtime implementation adds them.
No result here proves that those contracts are satisfactory.

The local `task check` runs the current HA member, Ruff, strict mypy, all tests, and the bootstrap and CI checkers.
It also runs the public-tree checker with history.
Set `PRIVATE_DENYLIST` to an external, nonempty private denylist before this command.
Do not commit that file.
Use `task ha-minimum` for the other HA member.

Use `task hassfest` and `task hacs` for the official container checks.
The HACS container uses `GITHUB_TOKEN` and `GITHUB_REPOSITORY`.
Use `task secrets` for Gitleaks history and file checks.
Use `actionlint` for workflow syntax checks.

The local `task package-check` examines the manifest and HACS fields for this package shape.
It also checks the package files and English config-flow strings.
It is not remote Marketplace validation or HACS-default acceptance.
Strict mypy checks `custom_components/assist_live` and `tools/` without import suppression.
The development environment includes HA imports from the installed package.

### HA test dependency sources

PyPI release metadata gives these mappings:

| Test package | HA requirement | Python requirement |
| --- | --- | --- |
| `pytest-homeassistant-custom-component==0.13.372` | `homeassistant==2026.10.1` | `>=3.14` |
| `pytest-homeassistant-custom-component==0.13.374` | `homeassistant==2026.10.3` | `>=3.14` |

Sources:

- <https://pypi.org/pypi/pytest-homeassistant-custom-component/0.13.372/json>
- <https://pypi.org/pypi/pytest-homeassistant-custom-component/0.13.374/json>

HA itself specifies Python `>=3.14.2`.
The Python floor and `.python-version` stay unchanged.
The development group pins `ruff==0.17.2`, `mypy==2.4.0`, `PyYAML==6.0.3`, and `types-PyYAML==6.0.12.20260906`.
Their PyPI `info.requires_python` values, in the same order, are `>=3.7`, `>=3.10`, `>=3.8`, and `>=3.10`.
The Ruff target is `py314` with rules `E`, `F`, `W`, `I`, `B`, and `UP`.
All rule sets stay active.

### GitHub Actions sources and pins

The GitHub REST tag data gave these commit IDs on 2026-10-24:

| Action | Tag | Commit |
| --- | --- | --- |
| `actions/checkout` | `v4.2.2` | `11bd71901bbe5b1630ceea73d27597364c9af683` |
| `astral-sh/setup-uv` | `v6.8.0` | `d0cc045d04ccac9d8b7881df0226f9e82c39688e` |
| `hacs/action` | `22.5.0` | `d556e736723344f83838d08488c983a15381059a` |
| `googleapis/release-please-action` | `v4.3.0` | `c2a5a2bd6a758a0937f1ddb1e8950609867ed15c` |

For each row, the source command is `gh api repos/OWNER/REPOSITORY/git/ref/tags/TAG`.
Each tag points directly to a commit, not an annotated tag.
The GitHub Actions definitions at these commits supply the inputs used in the workflows.

The only `home-assistant/actions` release tag, `1.0.0`, has no usable `hassfest/action.yml`.
The workflow pins the official `master` commit `cd6f3635a740c84d8a14f6121a90068b8ece6bf6`.
Sources: `gh api repos/home-assistant/actions/commits/master` and that commit's `hassfest/action.yml`.
The comment states the source branch and date, not an invented version.

This composite GitHub Actions step runs the moving image `ghcr.io/home-assistant/hassfest`.
The HACS GitHub Actions step also runs a moving image, `ghcr.io/hacs/action:main`.
The GitHub Actions SHAs do not pin these two image contents.

The HACS step ignores the `brands` check.
That check gives a failure if `custom_components/assist_live/brand/icon.png` and an entry in the Home Assistant brands repository are not available.
The project has no brand image at this time.
Source: `custom_components/hacs/validate/brands.py` in `hacs/integration`.

When the project adds a brand image that it owns, remove the ignore entry.
All other HACS checks stay mandatory.
If the repository has no description, the `description` check gives a failure.
If the repository has no topic, the `topics` check gives a failure.

Local validator image digests were:

```text
ghcr.io/home-assistant/hassfest@sha256:ac7d08c82d8b583ff7dc356faf993d08650cfc3015087cace566d65827a04c85
ghcr.io/hacs/action@sha256:187499a09c5d41f9f0d42ff80d203ed87b974c028290140913b61e005fb0aab2
```

The Gitleaks image uses the pin `zricethezav/gitleaks:v8.30.0@sha256:691af3c7c5a48b16f187ce3446d5f194838f91238f27270ed36eef6359a574d9`.
Source: `docker pull zricethezav/gitleaks:v8.30.0` and `docker image inspect`.
The Gitleaks configuration extends the default rules without path exemptions.
`Renovate` groups GitHub Actions and Python updates and pins GitHub Actions and image digests.
It runs each Monday before 06:00 UTC.
Automerge is off.

### Release authorization

The two release workflows run only from `workflow_dispatch`.
Their first step stops with a failure unless `vars.ASSIST_LIVE_RELEASES_APPROVED == 'true'`.
`ASSIST_LIVE_RELEASES_APPROVED` is not set.
The operator sets it only after publication approval and release authorization.
Only the release-please `job` has write permissions, for release proposals and releases.
Its manifest starts at `0.0.0`.

The archive workflow checks an archive in runner temporary storage and uploads nothing.
No push, release, or publication command ran during these checks.

### CI test-first result

Command from `$WORKSPACE/assist-live`:

```sh
uv run pytest tests/test_ci.py -q
```

Initial result before the CI checker was in the repository:

```text
E       ModuleNotFoundError: No module named 'tools.check_ci'
1 failed in 0.01s
```

The project metadata stayed unchanged for this test.

### Official validators

Commands from `$WORKSPACE/assist-live`:

```sh
docker run --rm -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest
docker run --rm -e INPUT_CATEGORY=integration -e GITHUB_REPOSITORY=legion-works/assist-live -v "$PWD":/github/workspace ghcr.io/hacs/action:main
```

Hassfest result:

```text
Integrations: 1
Invalid integrations: 0
```

No manifest change was necessary. The empty `codeowners` list was satisfactory.
The local HACS command stopped before validation:

```text
::ERROR:: No GitHub token found, use env GITHUB_TOKEN to set this.
```

HACS validation did not run locally: a token and a published repository are necessary.
CI runs it after publication, without exemptions or a skip condition.
This record makes no HACS acceptance claim.

### Local CI results

Date: 2026-10-24 (UTC).
Commands from `$WORKSPACE/assist-live`, with `PRIVATE_DENYLIST` set:

```sh
uv run pytest tests/test_ci.py -q && task check && task package-check && gitleaks dir . --redact --exit-code 1
actionlint
uv run --no-project --python 3.14.2 --with homeassistant==2026.10.1 --with pytest-homeassistant-custom-component==0.13.372 --with PyYAML==6.0.3 pytest tests/ -q
```

Result lines:

```text
14 passed in 0.33s
22 files already formatted
All checks passed!
Success: no issues found in 6 source files
78 passed in 2.43s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=2
PASS public data matches=0
PASS packaging (local check, not Marketplace or HACS-default acceptance)
INF no leaks found
PASS actionlint
78 passed in 2.47s
```

`actionlint` gave no diagnostics.
The last test result is the HA `2026.10.1` environment.
The locked development environment uses HA `2026.10.3` and test package `0.13.374`.
All 64 earlier tests stay unchanged and are part of each complete run.
The HA plugin test makes sure that pytest loads the HA test harness.

Static analysis reports no errors or warnings after selection of `.venv` in the Pyright configuration.
No import suppression is necessary.

Tests parse the workflow and make temporary copies with two changes:

- Remove `if: always()` from `ci-complete`.
- Remove `ha` from its `needs` list.

The CI checker rejects each copy with exit code `1`.
More tests reject a weakened `ci-complete` command and a missing matrix member.
The `ci-complete` simulation gave:

```text
no-always exit=1 FAIL CI completion condition
missing-needs exit=1 FAIL CI completion dependencies
HA minimum=success: PASS
HA minimum=skipped: FAIL
HA minimum=failure: FAIL
HA minimum=cancelled: FAIL
unit=success: PASS
unit=skipped: FAIL
unit=failure: FAIL
unit=cancelled: FAIL
PASS completion mutations and result simulation
```

The public-tree check reports 0 private-data findings in files and history.
A different literal search reports 0 denylist findings and 0 RFC 1918 addresses.
It reports one deliberate public URL test vector with the user-path substring.
The context-aware public-tree check accepts this vector.
The ASD-STE100 checker reports 0 findings for this file and the pull request template.

## Publication

Date: 2026-10-24 (UTC).

The project published three public repositories in the `legion-works` organization:

| Repository | First published revision | Visibility |
|---|---|---|
| `legion-works/assist-live` | `6422737` | `PUBLIC` |
| `legion-works/assist-live-server` | `df74b48` | `PUBLIC` |
| `legion-works/assist-live-esphome` | `4007354` | `PUBLIC` |

Checks on each repository before publication:

```sh
test "$(git rev-parse --show-toplevel)" = "$(pwd -P)"
task check
gitleaks dir . --redact --exit-code 1
gitleaks git . --redact --exit-code 1
uv run --project "$WORKSPACE/assist-live" python "$WORKSPACE/assist-live/tools/check_public_tree.py" --history --root . --private-denylist "$PRIVATE_DENYLIST"
git diff --check
git diff --cached --check
```

```text
PASS own Git root
no leaks found
PASS history secrets
PASS public data matches=0
```

Each repository has a description and topics, because the HACS `description` and `topics` checks use them.
The project publishes only `main`.

### First hosted CI runs

```sh
gh run view RUN_ID --repo legion-works/REPOSITORY --json conclusion,headSha,jobs
```

| Repository | Run | Revision | Result |
|---|---|---|---|
| `assist-live-server` | `40121627999` | `df74b48` | `success` |
| `assist-live` | `40121617665` | `6422737` | `failure` |
| `assist-live-esphome` | `40121638839` | `4007354` | `failure` |

In `assist-live`, the `unit` CI task and the two HA CI tasks each gave `16 failed, 62 passed`.
Each failure printed `FAIL history secrets missing_tool=1`.
The public-tree checker tests run Gitleaks, and hosted runners do not include it.
A local run without Gitleaks on the `PATH` gave the same `16 failed, 62 passed`.
These CI tasks install the pinned Gitleaks release at this time and check its SHA-256.

In `assist-live-esphome`, a Python 3.12 archive probe identified a source file as an archive.
The `assist-live-esphome` verification record gives the cause and the repair.

### CI runs after the repairs

| Repository | Run | Revision | Result |
|---|---|---|---|
| `assist-live` | `40124434376` | `135e98d` | `success` |
| `assist-live-esphome` | `40124438901` | `e4ef10f` | `success` |

In `assist-live`, these CI tasks gave `success`: `lint`, `typecheck`, `unit`, `HA 2026.10.1`, `HA 2026.10.3`, `hassfest`, `hacs`, `secrets`, and `ci-complete`.
The `hacs` CI task ran the hosted HACS validation with the `brands` check ignored.
In `assist-live-esphome`, `checks`, `compile (2026.10.0)`, and `ci-complete` gave `success`.

```sh
gh repo view legion-works/REPOSITORY --json nameWithOwner,visibility
```

```text
legion-works/assist-live PUBLIC
legion-works/assist-live-server PUBLIC
legion-works/assist-live-esphome PUBLIC
```

The release workflows stop unless the `ASSIST_LIVE_RELEASES_APPROVED` repository setting is `true`.
That setting is not set at this time.

## Corpus contract

Date: 2026-10-24.

The corpus tooling uses only the Python standard library.
No new package or external executable is necessary.
Tests make temporary WAV files with no voice.
They test a complete 650-record fixture, license and consent checks, partitions, sample limits, hashes, path safety, and freeze changes.
The temporary files do not supply human voice results.

The test-first command was:

```sh
uv run pytest tests/test_corpus.py -q
```

The initial result was:

```text
E       ModuleNotFoundError: No module named 'tools.corpus'
1 failed in 0.25s
```

After implementation, the same command gave:

```text
93 passed in 4.32s
```

The pause-interval test calculates two-sided Clopper-Pearson 95% limits with the standard library.
The maximum limit is 8.5176053% for 3 cuts in 100 samples and 5.6183548% for 9 cuts in 300 samples.
Only 0 or 1 cut satisfies the two limits at 100 samples.
The held-out pause experiment uses 300 samples selected before the experiment.
See [Corpus contract](testing/corpus.md) for the schema, commands, and arithmetic.

## Corpus supply

Date: 2026-10-24.

```text
BLOCKED corpus supply: no human recordings at this time
```

The P0 baseline and P1 acceptance stay blocked until an approved human corpus is available.
The committed human and synthetic input files each contain 0 rows.
The digest file records the empty metadata state, not corpus acceptance.
No dataset import, synthetic voice generation, or human recording occurred.
The operator must select and give approval for the corpus source.
Signed consent records and audio must stay external to Git.

With `CORPUS_AUDIO` set to an empty external directory, this command gives exit status 1:

```sh
uv run python tools/corpus.py check --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --synthetic tests/data/synthetic.jsonl --licenses tests/data/LICENSES.json --consent tests/data/consent-index.json --freeze tests/data/corpus.sha256
```

```text
FAIL held_out replay count 0 < 200
FAIL held_out replay speakers 0 < 10
FAIL held_out pause count 0 < 300
FAIL held_out pause speakers 0 < 10
FAIL held_out prefix count 0 < 30
FAIL tuning replay count 0 < 50
FAIL tuning pause count 0 < 100
FAIL tuning prefix count 0 < 30
```

The command does not change the digest file on failure.
The temporary complete-corpus test checks the three success lines and checks that `--verify-freeze` does not change the digest file.

## Corpus tooling checks

Date: 2026-10-24.

From `$WORKSPACE/assist-live`, the local gate was:

```sh
PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
```

```text
26 files already formatted
All checks passed!
Success: no issues found in 7 source files
171 passed in 11.80s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=6
PASS public data matches=0
```

The CI-version test commands were:

```sh
uv run --no-project --python 3.14.2 --with "homeassistant==2026.10.1" --with "pytest-homeassistant-custom-component==0.13.372" --with PyYAML==6.0.3 pytest tests/ -q
uv run --no-project --python 3.14.2 --with "homeassistant==2026.10.3" --with "pytest-homeassistant-custom-component==0.13.374" --with PyYAML==6.0.3 pytest tests/ -q
```

```text
171 passed in 11.49s
171 passed in 11.64s
```

The publication-file self-scan gave these counts:

```text
self-scan denylist=0 host_paths=1 rfc1918=0
```

The one host-path literal is a pre-existing public-URL test vector in the privacy checker tests.
The public-tree checker gave `PASS public data matches=0`.
No new external executable is necessary for the corpus tests or tool.

## Pipeline public interface

Date: 2026-10-24.

The initial test-first command from `$WORKSPACE/assist-live` was:

```sh
uv run pytest tests/test_pipeline_contract.py -q
```

```text
E       ModuleNotFoundError: No module named 'tools.probe_pipelines'
1 failed in 0.43s
```

The public-interface criterion is the component `__all__` export.
HA `2026.10.3` exports `async_create_default_pipeline` and `async_update_pipeline`.
The [pipeline report](research/q2-pipelines.md) gives the signatures, side effects, automatic setup sequence, and manual fallback.
The contract test checks each export and signature on the two HA versions in CI.

The HA harness checks creation, selected voice, changed engines, stable IDs, duplicate creation, and creation refusal.
It also checks two test-only satellites through their select entities.
The integration does not supply the duplicate-selection repair at this time.

The live command used explicit `--url`, `--token-file`, `--stt-engine`, `--tts-engine`, and `--tts-voice` arguments:

```sh
uv run python tools/probe_pipelines.py --url "$HA_TEST_URL" --token-file "$HA_TEST_TOKEN_FILE" --stt-engine "$HA_TEST_STT" --tts-engine "$HA_TEST_TTS" --tts-voice "$HA_TEST_VOICE"
```

The target was an isolated HA `2026.10.3` instance with an offline test provider.
The probe checked a non-null voice through creation, read, update, and read.
It did not run audio synthesis or use a debug endpoint.

```text
PASS live HA 2026.10.3 pipeline create/get/update with non-null voice
PASS cleanup: two probe pipelines deleted; pipeline IDs and preferred unchanged
```

An initial live run used the incorrect `pipeline` key for read and delete.
The correct public WebSocket key is `pipeline_id`.
The probe removed the one pipeline from that run and checked the list again:

```text
PASS initial probe pipeline removed; no probe pipelines remain
```

The WebSocket update schema accepts only the complete pipeline fields, not only the changed fields.
The corrected probe uses that schema.
The in-process update function accepts keyword updates.
The live probe is the preference and cleanup oracle.
HA does not export a preferred-pipeline getter through `__all__`.
Device trial not run: no test device at this time.

## Pipeline test dependencies

Date: 2026-10-24.

The HA wheel does not include all component requirements.
The first harness run found `ModuleNotFoundError` for `hassil` and `mutagen`.
The development group and CI use the requirements from these component manifests:

| Component | Requirement pins in HA 2026.10.1 and 2026.10.3 |
|---|---|
| `conversation` | `gazetteer-matcher==1.1.0`, `hassil==3.12.1`, `home-assistant-intents==2026.10.6` |
| `tts` | `mutagen==1.48.1` |
| `ffmpeg` | `ha-ffmpeg==3.2.2` |
| `assist_pipeline` | `pymicro-vad==1.0.1`, `pyspeex-noise==1.0.2` |

The manifests have the same pins on the two HA versions.
The transitive components `http`, `intent`, `llm`, `stt`, and `wake_word` have no more requirements in these versions.
Source URLs, accessed 2026-10-24:

- [HA 2026.10.1 component manifests](https://github.com/home-assistant/core/tree/2026.10.1/homeassistant/components).
- [HA 2026.10.3 component manifests](https://github.com/home-assistant/core/tree/2026.10.3/homeassistant/components).
- [pymicro-vad 1.0.1 package metadata](https://pypi.org/pypi/pymicro-vad/1.0.1/json).
- [pyspeex-noise 1.0.2 package metadata](https://pypi.org/pypi/pyspeex-noise/1.0.2/json).

The two compiled packages do not have CPython 3.14 Linux wheels on PyPI.
The `pymicro-vad` wheel list ends at CPython 3.12.
The `pyspeex-noise` release has only a source archive.
The lock file records the source archives and SHA-256 hashes:

```text
pymicro_vad-1.0.1.tar.gz
60e0508b338b694c7ad71c633c0da6fcd2678a88abb8e948b80fa68934965111
pyspeex_noise-1.0.2.tar.gz
56a888ca2ef7fdea2316aa7fad3636d2fcf5f4450f3a0db58caa7c10a614b254
```

The source archives use `setuptools`, `pybind11`, and a C++17 compiler.
Their `setup.py` files use `Pybind11Extension` with `language="c++"` and `cxx_std=17`.
They do not call `make` or `cmake`.
The initial local source build and dependency sync took 29.052 seconds.
The CI tasks check `gcc --version` and `g++ --version` before dependency setup.
The [GitHub runner image toolchain](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md) supplies those compilers.

An FFmpeg executable and audio conversion are not necessary for these tests.
HA `ffmpeg/__init__.py:58-69,119-133` does not refuse setup when the version query gives `None`.
The fixture replaces only the external FFmpeg version query, not HA imports or pipeline behavior.
The fixture also initializes `homeassistant` before `conversation` to load the exposed-entity configuration.

HA `http/server.py:290` uses a string key and causes an `aiohttp` `NotAppKeyWarning`.
HA `pyproject.toml:457-468` does not promote this warning to an error, but this project does.
The fixture asserts this warning only around HTTP setup with `pytest.warns`.
The assertion must stop the test if HA removes the warning. No global warning filter changes.

The harness does not open a WebSocket because HA HTTP authentication causes a different, once-per-key request warning.
The live instance supplies the WebSocket results.

## Pipeline checks

Date: 2026-10-24.

From `$WORKSPACE/assist-live`, the contract command was:

```sh
uv run pytest tests/test_pipeline_contract.py -q && uv run python tools/probe_pipelines.py --check --report docs/research/q2-pipelines.md
```

```text
7 passed in 0.44s
PASS Q2 pipeline contract
```

The repository gate was:

```sh
UV_LINK_MODE=copy PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
```

```text
29 files already formatted
All checks passed!
Success: no issues found in 8 source files
180 passed in 7.20s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=7
PASS public data matches=0
```

The CI-version commands were:

```sh
UV_LINK_MODE=copy uv run --no-project --python 3.14.2 --with "homeassistant==2026.10.1" --with "pytest-homeassistant-custom-component==0.13.372" --with PyYAML==6.0.3 --with "gazetteer-matcher==1.1.0" --with "ha-ffmpeg==3.2.2" --with "hassil==3.12.1" --with "home-assistant-intents==2026.10.6" --with "mutagen==1.48.1" --with "pymicro-vad==1.0.1" --with "pyspeex-noise==1.0.2" pytest tests/ -q
UV_LINK_MODE=copy uv run --no-project --python 3.14.2 --with "homeassistant==2026.10.3" --with "pytest-homeassistant-custom-component==0.13.374" --with PyYAML==6.0.3 --with "gazetteer-matcher==1.1.0" --with "ha-ffmpeg==3.2.2" --with "hassil==3.12.1" --with "home-assistant-intents==2026.10.6" --with "mutagen==1.48.1" --with "pymicro-vad==1.0.1" --with "pyspeex-noise==1.0.2" pytest tests/ -q
```

```text
180 passed in 12.70s
180 passed in 10.15s
```

The workflow syntax check `actionlint .github/workflows/ci.yml` had no output and exit code 0.
The ASD-STE100 checker had 0 findings for each changed Markdown file.
The Python and YAML language servers gave 0 errors and 0 warnings.
The Markdown, TOML, and lock files have no language-server diagnostic source.
The formatter, tests, and public-tree checker examine those files.

The publication-file self-scan gave these counts:

```text
self-scan denylist=0 host_paths=1 rfc1918=0
```

The one host-path literal is the same public-URL test vector in the privacy checker tests.
The public worktree command was:

```sh
uv run python tools/check_public_tree.py --root . --private-denylist "$PRIVATE_DENYLIST"
```

```text
PASS own Git root
PASS public data matches=0
```

## Marketplace release status

Date: 2026-10-24 (UTC).

The first test command from `$WORKSPACE/assist-live` was:

```sh
uv run pytest tests/test_marketplace.py -q
```

```text
E       ModuleNotFoundError: No module named 'tools.check_marketplace'
tests/test_marketplace.py:7: ModuleNotFoundError
1 failed in 0.22s
```

The explicit network command was:

```sh
uv run python tools/check_marketplace.py --minimum 2026.11 --live --record-status
```

```text
STATUS Q3 pending_release (recorded observation, not acceptance)
PASS Q3 status recorded
```

The [dated Marketplace record](research/q3-marketplace.md) gives the official sources and the pinned development source.
PyPI and GitHub reported `2026.10.4` as the latest stable release.
No stable `2026.11.x` release was in those responses.
The November release-note URL gave HTTP 404.

The rule document says that the Marketplace refuses repositories with HA OS apps.
The development source has an app-refusal branch only when the repository has no integration directory.
The released check must test app-only and mixed fixtures to resolve this difference.
The three-repository layout stays unchanged.
No Marketplace install trial ran. HACS validation is not Marketplace acceptance.

The offline status command was:

```sh
uv run pytest tests/test_marketplace.py -q && uv run python tools/check_marketplace.py --minimum 2026.11 --report docs/research/q3-marketplace.md --record-status
```

```text
25 passed in 0.34s
STATUS Q3 pending_release (recorded observation, not acceptance)
PASS Q3 status recorded
```

The acceptance command was:

```sh
uv run python tools/check_marketplace.py --minimum 2026.11 --report docs/research/q3-marketplace.md --require-accepted
```

```text
FAIL Q3 pending_release: HA 2026.11 has no recorded stable release
```

This command gave exit 1, as necessary before a release and actual install results.

The repository command was:

```sh
PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
```

```text
32 files already formatted
All checks passed!
Success: no issues found in 9 source files
205 passed in 6.12s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=8
PASS public data matches=0
```

The Python `3.14.2` matrix commands used the same explicit audio dependencies as the pipeline checks above:

```sh
uv run --no-project --python 3.14.2 --with homeassistant==2026.10.1 --with pytest-homeassistant-custom-component==0.13.372 --with PyYAML==6.0.3 --with gazetteer-matcher==1.1.0 --with ha-ffmpeg==3.2.2 --with hassil==3.12.1 --with home-assistant-intents==2026.10.6 --with mutagen==1.48.1 --with pymicro-vad==1.0.1 --with pyspeex-noise==1.0.2 pytest tests/ -q
uv run --no-project --python 3.14.2 --with homeassistant==2026.10.3 --with pytest-homeassistant-custom-component==0.13.374 --with PyYAML==6.0.3 --with gazetteer-matcher==1.1.0 --with ha-ffmpeg==3.2.2 --with hassil==3.12.1 --with home-assistant-intents==2026.10.6 --with mutagen==1.48.1 --with pymicro-vad==1.0.1 --with pyspeex-noise==1.0.2 pytest tests/ -q
```

```text
205 passed in 6.17s
205 passed in 6.11s
```

No new external executable is necessary for these tests or the probe.
The network mode uses the Python standard library and must have the explicit `--live` flag.
The repository-local `pyright tools/check_marketplace.py tests/test_marketplace.py` command gave:

```text
0 errors, 0 warnings, 0 informations
```

The editor diagnostic snapshot reported 15 unresolved-import errors for the new test module.
The repository-local Pyright command, mypy command, and unit tests resolved the module with no errors.
Markdown has no language-server diagnostic source.
The ASD-STE100 checker and privacy checker examine those documents.

The ASD-STE100 checker gave 0 findings for each changed Markdown file.
The publication-file self-scan gave:

```text
self-scan denylist=0 host_paths=1 rfc1918=0
```

The one host-path literal is an unchanged public-URL test vector in the privacy checker tests.
The worktree check command was:

```sh
uv run python tools/check_public_tree.py --root . --private-denylist "$PRIVATE_DENYLIST"
```

```text
PASS own Git root
PASS public data matches=0
```

## Marketplace acceptance record invariants

Date: 2026-10-24 (UTC).
The status stays `pending_release`. The three-repository layout stays unchanged.

The offline status gate must find the complete check-again section, its five steps, release condition, validator path, and acceptance command.
A released record must have a stable validator tag equal to `release_version` and a source URL with that tag.
For each official release source, `latest_stable` must be equal to or newer than `release_version`.
The checker compares versions as numeric tuples.

The rule quotation includes the full official paragraphs about the HACS custom integration and HA OS apps.
The live probe can use `GITHUB_TOKEN` or `GH_TOKEN` for HTTPS GitHub API requests.
The token does not go to other sources or into output.
An API rate limit gives the named `github_rate_limit` failure with token instructions.
Fake-opener tests check this behavior without network access.

The test and status command from `$WORKSPACE/assist-live` was:

```sh
uv run pytest tests/test_marketplace.py -q && uv run python tools/check_marketplace.py --minimum 2026.11 --report docs/research/q3-marketplace.md --record-status
```

```text
42 passed in 0.35s
STATUS Q3 pending_release (recorded observation, not acceptance)
PASS Q3 status recorded
```

The tests include a synthetic released-and-installed record that gives `PASS Q3 accepted` through `main()`.
This synthetic record is not an actual Marketplace install result.
The actual dated record gives exit 1 with this command:

```sh
uv run python tools/check_marketplace.py --minimum 2026.11 --report docs/research/q3-marketplace.md --require-accepted
```

```text
FAIL Q3 pending_release: HA 2026.11 has no recorded stable release
```

The mutation tests remove the full procedure or its first four steps and corrupt release-source or validator-tag data.
Each corrupted record must give exit 1:

```sh
uv run pytest tests/test_marketplace.py -q -k 'pending_report_requires or stale_latest_stable or nonrelease_validator_tag or dev_commit_url'
```

```text
11 passed, 31 deselected in 0.22s
```

The repository gate was:

```sh
PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
```

```text
32 files already formatted
All checks passed!
Success: no issues found in 9 source files
222 passed in 6.02s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=8
PASS public data matches=0
```

The Python `3.14.2` matrix commands above ran again with the same pinned dependencies.
The results for HA `2026.10.1` and HA `2026.10.3`, in that order, were:

```text
222 passed in 6.00s
222 passed in 5.99s
```

The network-isolated command was:

```sh
unshare -rn sh -c 'uv run pytest tests/test_marketplace.py -q && uv run python tools/check_marketplace.py --minimum 2026.11 --report docs/research/q3-marketplace.md --record-status'
```

```text
42 passed in 0.34s
STATUS Q3 pending_release (recorded observation, not acceptance)
PASS Q3 status recorded
```

The repository-root command `pyright tools/check_marketplace.py tests/test_marketplace.py` gave:

```text
0 errors, 0 warnings, 0 informations
```

The editor snapshot cannot resolve this module from the nested workspace root.
It reported 24 unresolved-import errors in the test module, with no checker-file errors or warnings.
Markdown has no language-server diagnostic source.
No import or configuration changes hide this difference.

## CI checks without and with a private denylist

Date: 2026-10-25 (UTC).
The public-tree check runs when no private denylist is set. It prints `NOT CHECKED private denylist`.
The check reads an external private denylist when `PRIVATE_DENYLIST` is set.

```sh
env -u PRIVATE_DENYLIST task check
PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
```

Each command gave `227 passed`, `PASS integration bootstrap`, `PASS CI`, `PASS own Git root`, and `PASS public data matches=0`.
The first command printed `NOT CHECKED private denylist`.
The second command printed `PASS history secrets commits=10` and did not print the unchecked status.

The HA matrix commands used the workflow requirements for HA `2026.10.1` and `2026.10.3`.
Each command gave `227 passed`.
The CI workflow check and `actionlint` gave `PASS`.

## CI parity repair checks

Date: 2026-10-25 (UTC).
The two task commands ran after the CI parity test changes:

```sh
env -u PRIVATE_DENYLIST task check
PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
```

Each command gave `32 files already formatted`, `All checks passed!`, `Success: no issues found in 9 source files`, `227 passed`, `PASS integration bootstrap`, `PASS CI`, `PASS own Git root`, and `PASS public data matches=0`.
The first command printed one `NOT CHECKED private denylist` line.
The second command printed `PASS history secrets commits=11` and no unchecked status.

The HA matrix commands ran again with the CI requirements:

```sh
export HA_VERSION=2026.10.1 PLUGIN_VERSION=0.13.372
uv run --no-project --python 3.14.2 --with "homeassistant==$HA_VERSION" --with "pytest-homeassistant-custom-component==$PLUGIN_VERSION" --with PyYAML==6.0.3 --with "gazetteer-matcher==1.1.0" --with "ha-ffmpeg==3.2.2" --with "hassil==3.12.1" --with "home-assistant-intents==2026.10.6" --with "mutagen==1.48.1" --with "pymicro-vad==1.0.1" --with "pyspeex-noise==1.0.2" pytest tests/ -q
export HA_VERSION=2026.10.3 PLUGIN_VERSION=0.13.374
uv run --no-project --python 3.14.2 --with "homeassistant==$HA_VERSION" --with "pytest-homeassistant-custom-component==$PLUGIN_VERSION" --with PyYAML==6.0.3 --with "gazetteer-matcher==1.1.0" --with "ha-ffmpeg==3.2.2" --with "hassil==3.12.1" --with "home-assistant-intents==2026.10.6" --with "mutagen==1.48.1" --with "pymicro-vad==1.0.1" --with "pyspeex-noise==1.0.2" pytest tests/ -q
```

Result: `227 passed in 7.22s` for HA `2026.10.1` and `227 passed in 7.42s` for HA `2026.10.3`.
`actionlint` and the CI workflow checker gave `PASS`.

## CI and private-denylist contract tests

Date: 2026-10-25 (UTC).
The baseline source with the test changes gave this result:

```sh
uv run --project "$WORKSPACE/assist-live" --locked pytest tests/test_ci.py tests/test_public_tree.py -q
```

Result: `1 failed, 83 passed in 2.86s`.
Only `test_checker_without_private_denylist_reports_unchecked` exited with code 2. The baseline checker gave an error because no `--private-denylist` value was set.
At baseline, the CI-to-Taskfile parity test gave `PASS`. It did not give a failure before this change.

Each CI mutation ran in a scratch copy. Table rows show test output for each command change, then for the unchanged CI command:

```text
BASELINE: 1 passed
M7: uv run --locked ruff format --check . -> uvx ruff format --check .: FAIL
M7 source reset: 1 passed
M8: uv run --locked mypy -> uvx mypy: FAIL
M8 source reset: 1 passed
M11: hassil==3.12.1 -> hassil==3.12.2: FAIL
M11 source reset: 1 passed
M3: pytest tests/ -q -> pytest tests -q: FAIL
M3 source reset: 1 passed
M13: add --no-sync to the CI HA command: FAIL
M13 source reset: 1 passed
```

After the change, `uv run --locked pytest tests/test_ci.py tests/test_public_tree.py -q` gave `84 passed`.
