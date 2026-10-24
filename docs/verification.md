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
