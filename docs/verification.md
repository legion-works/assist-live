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

## Timer replay import

Date: 2026-10-25.

The [corpus contract](testing/corpus.md) records the source URL, archive SHA-256, license text location, selection rule, and automatic boundary methods.
The source is Timers and Such v1.0, Zenodo record `4662102`, with human voice only.
The internal `LICENSE` starts with `CC0 1.0 Universal`.
The archive SHA-256 is `c92772c830109a6d685b2106fa3f28fc7b1184bc74722045282ed7efb0cf135a`.

The import kept 200 held-out replay rows and 50 tuning replay rows.
The held-out partition has 30 nominal sessions. The tuning partition has 33 other nominal sessions.
Recording IDs and complete audio hashes do not overlap.
All 250 WAV headers report mono 16 kHz, 16-bit PCM.
The source CSV supplies the transcript and the timer slots for `HassStartTimer`.

The source session IDs do not prove individual speaker identity.
The source test selection used age, gender, and fluency combinations, with truthful reports as an assumption.
No corpus freeze or manual boundary approval is in this record.

The difference between the energy method and local Whisper word timestamps is more than 50 ms for 181 recordings.
The external review sheet contains those recordings and a random sample of 20 recordings.
The two groups overlap in 12 recordings.
It contains 189 PNGs and an index. Manual review is pending.
The pause and prefix sets stay empty until volunteer collection.

### Test-first and unit checks

The command `uv run pytest tests/test_corpus.py -q` gave this result before the archive-source implementation:

```text
TypeError: fetch() got an unexpected keyword argument 'cache'
13 failed, 93 passed in 4.50s
```

More tests showed that cache-directory and cache-file symlinks could go into Git repositories.
The last corpus test command gave:

```text
113 passed in 8.33s
```

Thirteen temporary code mutations each caused a test failure.
These checks include archive and member hashes, source paths, cache paths, HTTPS redirects, WAV format, and intent names.
The synthetic ZIP fixtures use no network or external command.
No CI dependency changed.

### Imported audio checks

The audio and archive cache stay external to Git.
The commands use `CORPUS_AUDIO` and `CORPUS_CACHE` for those locations:

```sh
uv run python tools/corpus.py fetch --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO"
uv run python tools/corpus.py check --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --synthetic tests/data/synthetic.jsonl --licenses tests/data/LICENSES.json --consent tests/data/consent-index.json
```

```text
PASS fetched locators (hashes checked)
PASS held_out replay count 200 speakers 30
PASS tuning replay count 50 speakers 33
FAIL held_out pause count 0 < 300
FAIL held_out pause speakers 0 < 10
FAIL held_out prefix count 0 < 30
FAIL tuning pause count 0 < 100
FAIL tuning prefix count 0 < 30
```

The check exits with status 1 because of those five collection failures.
One more check with `--freeze` gave the same failures and wrote no output.
The committed `corpus.sha256` stayed unchanged. It does not pin the imported rows.

One more check independently compared all selected archive bytes, CSV transcripts, slot values, and WAV headers with the imported rows.
It recalculated each RMS endpoint with scalar arithmetic and checked each ASR endpoint and review flag.
It checked the PNG signatures and the index coverage.
The archive-member fetch also gave `PASS` with network access disabled and a new external audio root.

### Repository and HA checks

The command `PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check` gave:

```text
32 files already formatted
All checks passed!
Success: no issues found in 9 source files
242 passed in 9.53s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=10
PASS public data matches=0
```

The two Python 3.14.2 HA runs used the pinned optional-component packages from the CI contract:

```sh
uv run --no-project --python 3.14.2 --with homeassistant==2026.10.1 --with pytest-homeassistant-custom-component==0.13.372 --with PyYAML==6.0.3 --with gazetteer-matcher==1.1.0 --with ha-ffmpeg==3.2.2 --with hassil==3.12.1 --with home-assistant-intents==2026.10.6 --with mutagen==1.48.1 --with pymicro-vad==1.0.1 --with pyspeex-noise==1.0.2 pytest tests/ -q
uv run --no-project --python 3.14.2 --with homeassistant==2026.10.3 --with pytest-homeassistant-custom-component==0.13.374 --with PyYAML==6.0.3 --with gazetteer-matcher==1.1.0 --with ha-ffmpeg==3.2.2 --with hassil==3.12.1 --with home-assistant-intents==2026.10.6 --with mutagen==1.48.1 --with pymicro-vad==1.0.1 --with pyspeex-noise==1.0.2 pytest tests/ -q
```

```text
HA 2026.10.1: 242 passed in 8.95s
HA 2026.10.3: 242 passed in 8.30s
```

The self-scan counts were `denylist=0 private_ip=0 audio_files=0 absolute_home=1`.
The literal home-path result is a public URL test vector, not a local filesystem path.
The authoritative history and public-data checks gave the three PASS lines above.
The ASD-STE100 checker gave 0 findings for the corpus instructions.

## CTC boundary checks

Date: 2026-10-25.

The automatic boundary cross-check uses known-transcript `MMS_FA` CTC output from `torchaudio==2.7.1+cpu` and `torch==2.7.1+cpu`.
The aligner ran on CPU in an external Python `3.12.13` environment.
The [corpus instructions](testing/corpus.md#automatic-voice-boundaries) give the commands, model SHA-256, parameters, and sample conversion.
No aligner dependency or model weights are in the repository.

All 250 known transcripts gave CTC results.
The energy-minus-aligned median is `-77.53125 ms`, with p10 `-217.90625 ms`, p90 `69.15 ms`, and maximum `2128.375 ms`.
The maximum absolute difference is also `2128.375 ms`.
The paired median of CTC-end minus Whisper-end is `-1.78125 ms`.

The 50 tuning recordings have a median energy-minus-aligned difference of `3.3125 ms`.
This is below the `10 ms` systematic-bias threshold, so the energy parameters stay unchanged.
The held-out median is `-87.0625 ms`. No held-out statistic selected the energy parameters.
The English wav2vec2 CTC alternative gave 46 tuning flags, compared with 39 for `MMS_FA`.

The current rule flags 190 recordings. The prior Whisper rule flagged 181 recordings.
The causes overlap: 178 boundary differences, 64 low-confidence recordings, and 59 possible transcript differences.
The external review sheet has 195 PNGs with aligned word boundaries, word scores, and the two endpoint markers.
The same 20 random recordings stay in the sheet, with 15 also in the flagged group.

This result does not decrease manual review work.
A decision about the endpoint method stays necessary before acceptance.
The corpus is not frozen.

The command `uv run pytest tests/test_corpus.py -q -k 'ctc_boundary_flag or imported_ctc_flags'` first gave:

```text
ImportError: cannot import name 'boundary_review_reasons' from 'tools.corpus'
13 failed, 113 deselected in 0.54s
```

The full command `uv run pytest tests/test_corpus.py -q` gave:

```text
126 passed in 5.08s
```

All 19 code mutations caused a test failure.
Six mutations test the CTC difference limit, the two confidence limits, transcript mismatch, CTC failure, and flag consistency.
A check independently recalculated all RMS endpoints and compared all CTC words, sample limits, scores, and flags with the external model output.
It also checked all source bytes, transcripts, timer slots, PNG signatures, and review coverage.

The corpus command from the instructions gave:

```text
PASS held_out replay count 200 speakers 30
PASS tuning replay count 50 speakers 33
FAIL held_out pause count 0 < 300
FAIL held_out pause speakers 0 < 10
FAIL held_out prefix count 0 < 30
FAIL tuning pause count 0 < 100
FAIL tuning prefix count 0 < 30
```

The command with `--freeze` refused to write a manifest. The committed digest stayed unchanged.

The command `PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check` gave:

```text
32 files already formatted
All checks passed!
Success: no issues found in 9 source files
255 passed in 9.46s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=10
PASS public data matches=0
```

The two Python 3.14.2 HA matrix commands from the earlier record gave:

```text
HA 2026.10.1: 255 passed in 9.12s
HA 2026.10.3: 255 passed in 8.97s
```

## Local corpus boundaries and source checks

Date: 2026-10-25.

The local boundary tool builds one self-contained HTML file, imports manual samples, and compares automatic markers with manual samples.
The recording kit supplies consent text and 40 pause prompts for 400 recordings across two partitions.
No manual voice decision, volunteer recording, parameter change, or corpus freeze is complete.
The 30-minute estimate for 50 tuning rows is a planning estimate, not a measured result.

The first test command was:

```sh
uv run --locked pytest tests/test_corpus.py tests/test_annotate_corpus.py -q
```

The initial code did not have the declaration, source-record, download-limit, or manual-boundary guards:

```text
FAILED tests/test_corpus.py::test_missing_fields_are_rejected[last_voice_annotation]
AssertionError: assert []
32 failed, 127 passed in 5.97s
```

The archive provenance tests also ran before their implementation:

```sh
uv run --locked pytest tests/test_corpus.py -q -k archive_provenance
```

```text
14 failed, 145 deselected in 0.53s
```

The same corpus test command after implementation gave:

```text
179 passed in 8.85s
```

Nineteen numbered mutation tests stopped at their intended assertions.
These tests remove the declaration, automatic method, archive provenance, source-record, license-byte, download-limit, and manual-flag guards.
They also remove import checks for the annotator, duplicates, sample types and limits, hashes, lengths, fields, partition, notes, and version.
One test checks the skip no-op.
The held-out parameter-writer mutation is not applicable because `compare` has no parameter writer.
Unknown IDs also cause failure at the necessary record lookup, independently of the explicit guard.

The browser check used a synthetic tone, not human audio.
Headless Chromium checked waveform clicks, 10 ms and 1 ms keys, the 500 ms loop, JSON export, and storage after reload.
The browser check is external to CI and does not add a test dependency.
The 50-row tuning page built with:

```sh
uv run --locked python tools/annotate_corpus.py build --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --split tuning --output "$ANNOTATION_DIR"
```

```text
PASS annotation page rows=50
```

The output path stays external to the repository because the HTML file includes audio.
The cached fetch used the license digest and the archive pin:

```sh
uv run --locked python tools/corpus.py fetch --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --cache "$CORPUS_CACHE"
uv run --locked python tools/corpus.py check --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --synthetic tests/data/synthetic.jsonl --licenses tests/data/LICENSES.json --consent tests/data/consent-index.json
```

```text
PASS fetched locators (hashes checked)
PASS held_out replay count 200 speakers 30
PASS tuning replay count 50 speakers 33
FAIL held_out pause count 0 < 300
FAIL held_out pause speakers 0 < 10
FAIL held_out prefix count 0 < 30
FAIL tuning pause count 0 < 100
FAIL tuning prefix count 0 < 30
```

The corpus check exits 1 for those five collection shortages. It does not write a freeze.

The repository command was `PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check`:

```text
35 files already formatted
All checks passed!
Success: no issues found in 10 source files
313 passed in 12.74s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=15
PASS public data matches=0
```

The floor-Python HA commands agree with the shipped CI dependencies:

```sh
uv run --no-project --python 3.14.2 --with homeassistant==2026.10.1 --with pytest-homeassistant-custom-component==0.13.372 --with PyYAML==6.0.3 --with gazetteer-matcher==1.1.0 --with ha-ffmpeg==3.2.2 --with hassil==3.12.1 --with home-assistant-intents==2026.10.6 --with mutagen==1.48.1 --with pymicro-vad==1.0.1 --with pyspeex-noise==1.0.2 pytest tests/ -q
uv run --no-project --python 3.14.2 --with homeassistant==2026.10.3 --with pytest-homeassistant-custom-component==0.13.374 --with PyYAML==6.0.3 --with gazetteer-matcher==1.1.0 --with ha-ffmpeg==3.2.2 --with hassil==3.12.1 --with home-assistant-intents==2026.10.6 --with mutagen==1.48.1 --with pymicro-vad==1.0.1 --with pyspeex-noise==1.0.2 pytest tests/ -q
```

```text
HA 2026.10.1: 313 passed in 13.48s
HA 2026.10.3: 313 passed in 12.18s
```

The ASD-STE100 checker gave 0 findings for the changed Markdown files.
The public-tree check gave `PASS public data matches=0`.
The fixed-string self-scan found no denylist terms, private IPv4 addresses, or audio files.
Its one literal home-path result is the unchanged public URL test vector.
No new external test tool or project dependency is necessary.

## Pause interval fields

Date: 2026-10-25.

The corpus schema checks `pause_intervals_ms`, `executable_prefix_intervals_ms`, and the necessary `speaker_type` field.
The page marks pauses and executable-prefix boundaries without a change to the last voice sample.
Import writes the manual interval lists and the first pause sample boundaries.
The `compare` command accepts tuning rows only.

The initial test command was:

```sh
uv run --locked pytest tests/test_corpus.py tests/test_annotate_corpus.py -q
```

Its result was `35 failed, 188 passed`.
The missing-field tests reported `AssertionError: assert []` for the three necessary fields.
The held-out `compare` test also gave an unsatisfactory result before the restriction: `AssertionError: assert 0 != 0`.

The complete repository check command was:

```sh
PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
```

```text
35 files already formatted
All checks passed!
Success: no issues found in 10 source files
365 passed in 11.89s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=17
PASS public data matches=0
```

The two floor-Python commands were:

```sh
uv run --no-project --python 3.14.2 --with homeassistant==2026.10.1 --with pytest-homeassistant-custom-component==0.13.372 --with PyYAML==6.0.3 --with gazetteer-matcher==1.1.0 --with ha-ffmpeg==3.2.2 --with hassil==3.12.1 --with home-assistant-intents==2026.10.6 --with mutagen==1.48.1 --with pymicro-vad==1.0.1 --with pyspeex-noise==1.0.2 pytest tests/ -q
uv run --no-project --python 3.14.2 --with homeassistant==2026.10.3 --with pytest-homeassistant-custom-component==0.13.374 --with PyYAML==6.0.3 --with gazetteer-matcher==1.1.0 --with ha-ffmpeg==3.2.2 --with hassil==3.12.1 --with home-assistant-intents==2026.10.6 --with mutagen==1.48.1 --with pymicro-vad==1.0.1 --with pyspeex-noise==1.0.2 pytest tests/ -q
```

```text
HA 2026.10.1: 365 passed in 11.71s
HA 2026.10.3: 365 passed in 12.09s
```

The corpus command used the pinned archive cache and an external audio directory:

```sh
uv run --locked python tools/corpus.py fetch --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --cache "$CORPUS_CACHE"
uv run --locked python tools/corpus.py check --labels tests/data/labels.jsonl --audio-root "$CORPUS_AUDIO" --synthetic tests/data/synthetic.jsonl --licenses tests/data/LICENSES.json --consent tests/data/consent-index.json
```

```text
PASS fetched locators (hashes checked)
PASS held_out replay count 200 speakers 30
PASS tuning replay count 50 speakers 33
FAIL held_out pause count 0 < 300
FAIL held_out pause speakers 0 < 10
FAIL held_out prefix count 0 < 30
FAIL tuning pause count 0 < 100
FAIL tuning prefix count 0 < 30
```

The five collection shortages stay unchanged. No corpus freeze is complete.
All 250 replay rows declared `speaker_type=human` before this schema change.
The complete `labels.jsonl` file is byte-identical, with SHA-256 `9612197a4e95598935844d8236d1732da0001388db78e2d14d40bea0c2283f6c`.

Temporary synthetic WAV files supplied a pause row and a prefix row through the page and importer.
The imported rows satisfy the server policy rules in the test fixture, with no cross-repository dependency.
The fixture cites `assist-live-server/tools/tune_profiles.py` at commit `6f8b3b07195040d0a8d43ee4123e5f8f83b96283`.
A script checked the page controls and JSON export. No browser trial is complete.
No new external program or dependency is necessary for the repository tests.

Seventeen numbered mutations parsed correctly and gave unsatisfactory results in their target tests.
They include necessary fields, interval lists and pairs, numeric boundaries, time limits, order, overlap, import checks, manual declarations, and tuning-only `compare`.
The production files stayed unchanged during these mutation tests.

The ASD-STE100 check gave 0 findings for the changed Markdown files.
The privacy self-scan found 0 private terms, 0 private IPv4 addresses, and 0 audio files.
Its one literal home-path result is the unchanged public URL test vector.
The public-tree check gave `PASS public data matches=0`.

## Per-satellite voice forwarding

Date: 2026-10-25 (UTC).

The offline HA tests use two anonymous satellite keys.
They check turn separation, announcements, follow-ups, restart markers, fallback metadata, and the selected pipeline voice.
Two input-cancellation tests check that a stopped server sender does not remove future audio or text.
The runtime test adds the entity pair to the selected device and saves a stable pipeline ID.

The first forwarding test run gave this result before the runtime modules were available:

```text
ModuleNotFoundError: No module named 'custom_components.assist_live.ledger'
1 failed, 14 errors
```

Commands from the repository root:

```sh
uv run --locked pytest tests/test_config_flow.py tests/test_stt_forward.py tests/test_tts_forward.py tests/test_ledger.py tests/test_repairs.py tests/test_tts_lookup_contract.py -q
PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
task package-check
task hassfest
actionlint
```

Results:

```text
26 passed in 0.45s
48 files already formatted
All checks passed!
Success: no issues found in 15 source files
419 passed in 11.03s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=19
PASS public data matches=0
PASS packaging (local check, not Marketplace or HACS-default acceptance)
Integrations: 1
Invalid integrations: 0
```

`actionlint` gave exit code 0 with no output.
Hassfest accepts the unreleased client pin. This is not a client release or an installation claim.

### HA version matrix

Each command uses Python 3.14.2.
The first matrix commands used client source `051bc1bb1dabf0fdaa76fee0736b97128b265ab3`.

```sh
task ha-minimum
uv run --no-project --python 3.14.2 --with homeassistant==2026.10.3 --with pytest-homeassistant-custom-component==0.13.374 --with PyYAML==6.0.3 --with gazetteer-matcher==1.1.0 --with ha-ffmpeg==3.2.2 --with hassil==3.12.1 --with home-assistant-intents==2026.10.6 --with mutagen==1.48.1 --with pymicro-vad==1.0.1 --with pyspeex-noise==1.0.2 --with 'assist-live-client @ git+https://github.com/legion-works/assist-live-server@051bc1bb1dabf0fdaa76fee0736b97128b265ab3#subdirectory=client' pytest tests/ -q
```

The HA 2026.10.3 command gave `419 passed in 10.94s`.
The TTS lookup signature test runs in that suite.

The minimum command cannot resolve its dependencies:

```text
error: No solution found when resolving `--with` dependencies
cause: Because homeassistant==2026.10.1 depends on aiohttp==3.14.4 and assist-live-client==0.0.0 depends on aiohttp==3.14.5, we can conclude that assist-live-client==0.0.0 and homeassistant==2026.10.1 are incompatible.
And because only assist-live-client==0.0.0 is available, we can conclude that all versions of assist-live-client and homeassistant==2026.10.1 are incompatible.
And because you require homeassistant==2026.10.1 and assist-live-client, we can conclude that your requirements are unsatisfiable.
```

The minimum-version suite did not run with that client source.
The checks did not override the dependency conflict.

Client source `8622bf1ac781227e900e98d0a04a6ff9023ad625` declares `aiohttp>=3.14.4,<4`.
The four Git source pins and the lock use that commit.
The manifest keeps `assist-live-client==0.0.0`.
The same matrix commands, with that new client source, gave these results:

```text
HA 2026.10.1: 421 passed in 11.42s
HA 2026.10.3: 421 passed in 12.01s
```

Each suite includes the TTS lookup signature contract.

### Workflow and corpus guards

The mandatory CI `secrets` task runs the public-tree history check for this repository.
Its Gitleaks installation uses the same checked 8.30.0 archive as the test tasks.
The two test tasks install Node 22.22.0 for the page callback test.
The CI checker compares each installation to one version and checksum template.
The page test gives a named assertion if Node is missing or its version does not start with 22.

The corpus checker names a missing interval field as `missing`.
The page does not accept a prefix boundary without a finite pause end.
The copied server policy test states its sync rule.

Twenty-one isolated mutations gave test failures, with no collection errors.
They include closure on `idle`, cross-satellite closure, a consumed TTS generator, voice removal, and external VAD.
They also include removal of the public-tree step, weak manifest pins, obsolete flow strings, and installation checksum changes.
The production files did not change during these probes.

The privacy self-scan gave 0 private terms, 0 private IPv4 addresses, and 0 audio files.
Its one literal home-path result is the unchanged public URL test vector.
The public-tree checker gave `PASS public data matches=0`.

Device trials have not run. No test hardware is available at this time.
Sentence-boundary continuation is not available through the current client API.

### Last local checks

The full forwarding command, `task check`, and `task package-check` gave these results with the compatible client source:

```text
28 passed in 0.52s
48 files already formatted
All checks passed!
Success: no issues found in 15 source files
421 passed in 12.15s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=19
PASS public data matches=0
PASS packaging (local check, not Marketplace or HACS-default acceptance)
```

Hassfest gave `Invalid integrations: 0`.
`actionlint` gave exit code 0 with no output.

## Virtual conversation and operation records

Date: 2026-10-25.

The initial harness test used this command:

```sh
uv run --locked pytest tests/test_test_environment.py -q
```

It gave this failure before the tool was available:

```text
E ModuleNotFoundError: No module named 'tools.test_environment'
1 failed
```

The harness gave `14 passed in 0.52s` with the same command.
The tests use the stock HA conversation path for timers, service calls, exposure rules, areas, and commands with time intervals.
They check the actual long-response delta stream and make sure that short responses give no deltas.

With `ASSIST_LIVE_TEST_TOKEN_FILE` set to an external test credential file, this command ran on the isolated HA test container:

```sh
uv run python tools/test_environment.py --labels tests/data/labels.jsonl --split held_out --install-virtual --check-evidence
```

```text
PASS virtual intent coverage
PASS short and long groups
PASS transcript intent action evidence
```

The run used 200 held-out commands, with 100 commands in each reply group and 400 records across the two phase runs.
These are text-path checks, not audio replay or latency measurements.
The public config-entry API also gave satisfactory single-entry, removal, and installation checks.
See [Virtual intent tests](testing/virtual-intents.md) for the record contract and limits.

## Stored state and contributor checks

Date: 2026-10-25.

Stored turn data has payload version 1.
The reader accepts the prior dictionary and list shapes.
Tests show that invalid keys, row fields, cursor types, and timing values do not stop setup.
Correct rows and cursors stay available.
A generic log line and an HA repair report invalid stored entries without their content.

The private-denylist tests check unset, correct, missing-file, and denied inputs through the Taskfile command.
An unset `PRIVATE_DENYLIST` prints `NOT CHECKED private denylist` and keeps all other checks.
A set `PRIVATE_DENYLIST` keeps failure behavior for unavailable input and denied content.
This repository command checks the record, response-group, stream, stored-state, fixture-source, and private-denylist guards:

```sh
uv run --locked pytest tests/test_test_environment.py tests/test_config_flow.py tests/test_ci.py -q
```

## Client source and HA checks

Date: 2026-10-25.

The development client source is `961cacb31a013fceee612b245ea9c3cb5d745106`.
The lockfile, Taskfile, and CI use this source.
The release manifest requirement stays at `assist-live-client==0.0.0`.

```sh
PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
env -u PRIVATE_DENYLIST task check
task ha-minimum
task package-check
task hassfest
```

The current and minimum HA suites each gave `475 passed`.
The current suite uses HA 2026.10.3. The minimum suite uses HA 2026.10.1 and Python 3.14.2.
The current code checks gave these lines:

```text
54 files already formatted
All checks passed!
Success: no issues found in 16 source files
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=21
PASS public data matches=0
PASS packaging (local check, not Marketplace or HACS-default acceptance)
```

The check without the private denylist also printed `NOT CHECKED private denylist`.
Hassfest gave `Integrations: 1` and `Invalid integrations: 0`.

## Package files and virtual record boundaries

Date: 2026-10-25.

The package checker has an explicit source-file set.
In a Git worktree, it checks files in the Git index and untracked files that Git does not ignore.
Without Git metadata, it checks all files in the package folder.
No Python bytecode file is in the permitted set.
Tests check nested agent copies, unexpected files, ignored bytecode, bytecode in the Git index, and archive contents.

The stored-state tests also check a correct satellite name that is not in the config entry.
The fixture-source tests remove each necessary agent file independently.
Each missing file gives one named failure.

The replayed corpus text is not an STT result.
The intent and operation check does not compare the input transcript with itself.
The public inventory check uses entity states and identifiers on the selected timer device.

```sh
uv run --locked pytest tests/test_test_environment.py tests/test_config_flow.py tests/test_ci.py -q
uv run --locked pytest tests/test_test_environment.py -q
```

```text
95 passed in 5.80s
22 passed in 0.59s
```

The isolated-container command in [Virtual intent tests](testing/virtual-intents.md) gave the unchanged result lines:

```text
PASS virtual intent coverage
PASS short and long groups
PASS transcript intent action evidence
```

The full gates used these commands:

```sh
PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
env -u PRIVATE_DENYLIST task check
task ha-minimum
task package-check
task hassfest
```

The private-denylist run gave `496 passed in 24.03s`.
The run without that input gave `496 passed in 20.44s` and `NOT CHECKED private denylist`.
The minimum HA run gave `496 passed in 21.65s`.
The other result lines were:

```text
54 files already formatted
All checks passed!
Success: no issues found in 16 source files
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=22
PASS public data matches=0
PASS packaging (local check, not Marketplace or HACS-default acceptance)
Integrations: 1
Invalid integrations: 0
```

## Bounded STT fallback

Date: 2026-10-25 (UTC).

Each turn has one bounded audio buffer and one initial clock.
The checks include 199/201 ms for health, 599/601 ms for server messages, 15 s for audio, and 20 s for fallback.
They also include replay order, byte limits, local voice history, engine properties, circular selection, cancellation, and complete transcripts.
The time checks use a controlled event-loop clock.

The first test run used this command:

```sh
cd "$WORKSPACE/assist-live"
uv run pytest tests/test_stt_fallback.py -q --tb=short
```

The tests gave failure results for missing fallback behavior.
The first clock fixture could not use an active event loop.
After that fixture correction, the result for the selected tests was:

```text
ModuleNotFoundError: No module named 'custom_components.assist_live.stt_fallback'
AssertionError: assert False
3 failed, 1 passed, 16 deselected, 1 error in 0.45s
```

The assertion checks that the health path ends after 201 ms.
The completed implementation gave these results:

```sh
uv run pytest tests/test_stt_fallback.py tests/test_stt_forward.py tests/test_ledger.py -q
PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
task ha-minimum
task hassfest
```

```text
55 passed in 2.04s
57 files already formatted
All checks passed!
Success: no issues found in 17 source files
527 passed in 23.57s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=23
PASS public data matches=0
527 passed in 24.09s
Integrations: 1
Invalid integrations: 0
```

Eight numbered changes each gave a test failure:

1. The fallback deadline starts again at failover.
2. The first live audio chunk occurs two times.
3. The kept audio does not contain one byte.
4. Local audio restarts the server-message timer.
5. Fallback starts after a complete server transcript.
6. An error result supplies command text that is not complete.
7. The audio buffer has no byte limit.
8. The fallback chain accepts an assist-live entity.

These checks ran changed modules in memory. They did not change source files.
The `stage` values `fallback_used`, `stt_final`, and `stt_failed` apply without a new value.
No device or household recording was part of these tests.
Device measurements stay open because no test device is available.

## Native TTS format

Date: 2026-10-25.

The client source pin is `ee404d406942fbeb64d6c2380366eb380a2ca403` in the project metadata, lockfile, Taskfile, and CI workflow.

The manifest has the requirement `assist-live-client==0.0.0`.

The TTS entity reads the declared PCM format before it writes audio. It calculates the WAV byte rate and the number of bytes for each sample frame from that format.

The entity counts sentence-end items in request-local state. The current ledger step names cannot store this count. Resume after the last complete sentence is not part of this change.

If server audio has a failure after the first audio item, the entity stops the stream and reports `assist_live_tts_failed`. It does not continue at the next sentence.

The first typed-stream test run, before the client pin changed, gave:

```text
6 failed, 8 passed
```

After the client pin changed and before the entity used `tts_stream()`, the TTS test run gave:

```text
6 failed, 8 passed
```

The TTS command after the implementation was:

```sh
uv run --locked pytest tests/test_tts_forward.py -q
```

```text
15 passed in 0.70s
```

The live probe used the pinned server source and the read-only Piper voice files.

The scratch source copy had the same 112 files as the pinned revision.

The integration TTS entity sent a short text to the server. The reply began with a 44-byte WAV header. Its sample rate was 22050 Hz. Its byte rate was 44100. Its sample frame size was 2 bytes.

The reply had 73216 audio bytes. At 44100 bytes for each second, this is 1.660 seconds of audio.

The header bytes agree with the values from `struct.pack`.

An inert `backend=object()` value was the probe sentinel.

The health route reports unhealthy when only `tts_provider` is set.

The TTS audio route accepts the provider. The probe sent TTS to Piper. The next server writer must repair the health route.

The live probe used the integration project configuration and a temporary test. The test used only local socket connections.

The five mutations changed one behavior at a time. Each test for this work identified the incorrect result:

| Mutation | Test result |
|---|---|
| Set the 22050 Hz rate to 16000 Hz | The 22050 Hz header test reported byte 24 as incorrect. |
| Set the byte rate to 32000 | The 22050 Hz header test reported byte 28 as incorrect. |
| Accepted audio before the format item | The audio-before-format test got an assertion error, not fallback. |
| Played an unsupported codec | The unsupported-format test received server audio, not fallback. |
| Dropped the selected voice | The pipeline-voice test found an empty voice. |

The count stays in local request state. This change does not add a sentence-end step to the ledger.

## Audio failure paths and client source checks

Date: 2026-10-26.

The TTS entity selects fallback when the server sends a format without an audio item.
The WAV header uses the validated format values for 16000 Hz or 22050 Hz PCM.
The entity accepts sentence-end items before and after audio without a sentence counter.
Continuation at a sentence boundary is not available in the integration.

Unexpected STT and TTS failures give one warning with the error type, not the error message.
Tests plant sensitive text in error messages and check that it does not show in the logs.
The 200 ms STT health limit starts at the health call, after the turn-store save.
The audio-input deadline test has its own time limit.

The CI checker reads the client source revision from `pyproject.toml`.
It compares that revision with the lockfile, Taskfile, and CI workflow.
Each mismatch gives an error that names the file.

Before the code changes, the command was:

```sh
uv run --locked pytest tests/test_tts_forward.py tests/test_stt_fallback.py tests/test_ci.py -q
```

```text
13 failed, 101 passed in 2.58s
```

The first run included CI tests for an unavailable root argument.
The tests use an isolated module path and the same checker interface.
Before the checker changes, the CI tests gave:

```sh
uv run --locked pytest tests/test_ci.py -k client_pin -q --tb=short
```

```text
Failed: DID NOT RAISE <class 'ValueError'>
ValueError: HA component requirements command
4 failed, 46 deselected in 0.49s
```

After the code changes, the same command gave:

```text
113 passed in 4.26s
```

Two more lockfile tests check the declared revision and resolved commit independently.
The full suite includes these tests:

```sh
PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
task ha-minimum
task package-check
task hassfest
```

```text
57 files already formatted
All checks passed!
Success: no issues found in 17 source files
556 passed in 16.91s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=28
PASS public data matches=0
556 passed in 16.62s
PASS packaging (local check, not Marketplace or HACS-default acceptance)
Integrations: 1
Invalid integrations: 0
```

Twenty isolated mutations each caused a test failure.
These included missing warnings, error-message disclosure, an empty audio stream, unsupported rates, and incorrect WAV fields.
They also included sentence stops, a turn-relative health limit, and missing source-pin checks.
With a 60 s input limit, the deadline test gave `TimeoutError` in 1.544 s, with test startup.

The removed counter had no reader and no effect on output.
Its removal does not make a new behavior test necessary.
The sentence-end tests check the output before and after audio.
Human-readable instructions have no executable behavior test.
The setup instructions agree with the client format and sentence-end items.
The STT instructions state the 15 s limit from the start of the turn.

## Streamed TTS continuation

Date: 2026-10-26 (UTC).

The entity sends each text part without a full-reply wait and keeps a bounded copy for fallback.
The copy limit is 16,384 characters. The limit does not count UTF-8 bytes.
The health check has its own 200 ms limit and checks the selected audio function.
An older server without function flags selects fallback.

The first contract run used:

```sh
uv run pytest tests/test_tts_stream.py tests/test_tts_fallback.py -q
```

```text
14 failed, 12 passed in 1.19s
ModuleNotFoundError: No module named 'custom_components.assist_live.diagnostics'
```

The initial tests also found missing continuation, voice defaults, and notification settings.
A non-ASCII test found the byte-count error before the character-count change:

```sh
uv run pytest tests/test_tts_stream.py::test_two_byte_text_at_character_limit_is_accepted -q
```

```text
HomeAssistantError: TTS text limit is 16384 UTF-8 bytes
1 failed in 0.28s
```

The current contract checks use:

```sh
uv run pytest tests/test_tts_stream.py tests/test_tts_fallback.py tests/test_tts_lookup_contract.py -q
PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check
```

```text
46 passed in 0.64s
62 files already formatted
All checks passed!
Success: no issues found in 19 source files
599 passed in 17.30s
PASS integration bootstrap
PASS CI
PASS own Git root
PASS history secrets commits=29
PASS public data matches=0
```

Python 3.14.2 and HA 2026.10.3 supplied this local test run.
The private TTS lookup contract stays in the minimum-HA test matrix.
The client source pin is `8f3d60a4bb19122a760ff22ae83f7be68f1f95e6`.
The manifest requirement stays `assist-live-client==0.0.0`.

Continuation uses the last complete sentence and the same audio format.
The fallback WAV parser removes the header and checks the PCM format before output.
No complete sentence, an empty fallback, or a format difference stops the audio and reports the full reply through the failure event.
The optional notification uses the public `persistent_notification.create` service with `message` and `title`.
It does not run the conversation or the device operation again.
Diagnostics contain failure counts and fallback modes, without reply text or credentials.

Thirteen isolated mutations each caused a test failure.
The checks included full-text waits, an exhausted generator, duplicate parts, format differences, and a kept WAV header.
They also included an operation run again, a missing voice, an ignored function flag, and credentials in diagnostics.
The remaining checks included byte counts, notification settings, sentence markers, and type-only error logs.

## Complete TTS replies and transport errors

Date: 2026-10-26 (UTC).

A server error after the last complete sentence does not mean that reply text is missing.
The diagnostics counter `late_server_failures` counts this condition until integration setup starts again.
The integration keeps the type-only error log.
It does not mark `tts_failed`, fire `assist_live_tts_failed`, send a notification, or start fallback.
The WAV parser skips other RIFF chunks before `data` and uses only `fmt` and `data`.

The first test run used:

```sh
uv run pytest tests/test_tts_fallback.py::test_complete_reply_counts_only_late_server_errors -q
```

```text
KeyError: 'late_server_failures'
2 failed in 0.30s
```

The same command after the counter change gave:

```text
2 passed in 0.26s
```

The test checks the usual end and a server error after all sentence markers.
It checks two requests, correct audio, the diagnostics count, and no failure event or notification.
It also checks that logs contain only the error type.
An isolated mutation removed the counter increment.
The source stayed correct Python, and the tests gave `1 failed, 1 passed` with `assert 0 == 1`.

The TTS contract command used:

```sh
uv run pytest tests/test_tts_stream.py tests/test_tts_fallback.py tests/test_tts_lookup_contract.py -q
```

```text
48 passed in 0.64s
```

## Server text positions for TTS continuation

Date: 2026-10-26 (UTC).

The client source pin is `b4a920241e4131df34df678de1faab5e9cec59b0`.

The [server protocol](https://github.com/legion-works/assist-live-server/blob/b4a920241e4131df34df678de1faab5e9cec59b0/docs/protocol.md) states:

"The last position lets the fallback continue from `full_text[text_end:]` when the audio formats agree."

It also states: "Older peers reject unknown keys in the two directions."
The entity uses its current health result to request `text_positions` only when the server advertises that function.
It uses the last marker position and removes whitespace at the start of the remaining text.
It has no local punctuation or abbreviation rules.

The first test run used:

```sh
uv run pytest tests/test_tts_fallback.py -q -k 'server_position or missing_position or oversized_position'
```

```text
E       assert [False] == [True]
18 failed, 23 deselected in 0.69s
```

The tests use fake client streams for positions before and after the first audio.
They check abbreviations, attached quotation marks, emoji, an astral character, `0`, and two successive markers.
The fallback receives only the correct remaining text, one time, without a second WAV header.
The tests also check one health read and one read of each HA text part.

An older server, a missing position, and a last marker without a position use the full-text failure event.
A fake WebSocket supplies a position greater than the sent text length to the client decoder.
The decoder gives `ProtocolError`, and the entity sends the full-text failure event without fallback audio.

The implementation check used:

```sh
uv run pytest tests/test_tts_stream.py tests/test_tts_fallback.py tests/test_tts_forward.py -q
```

```text
88 passed in 1.08s
```

Eight temporary mutations each caused test failures:

| Mutation | Result |
| --- | --- |
| Disable the position request | `17 failed, 1 passed` |
| Request positions from an older server | `1 failed` |
| Add one to the position | `2 failed, 12 passed` |
| Subtract one from the position | `14 failed` |
| Keep a previous position after a marker without one | `1 failed` |
| Start with position `0` before a marker | `1 failed` |
| Keep whitespace at the start of the remaining text | `12 failed, 2 passed` |
| Replay the full text when a marker has no position | `3 failed` |

The source has none of these mutations.
The format checks, header removal, empty WAV refusal, cancellation, and late-error counter keep their tests.

## HA modes, destinations, and last-turn latency

Date: 2026-10-27 (UTC).

The client source pin is `b4a920241e4131df34df678de1faab5e9cec59b0`, with package version `0.0.0`.
HA local-only checks include HA fallbacks and speaker events, not server provider selection.
The server has its own local-only setting. The pinned health response does not show that setting.

Diagnostics show server destinations only after a turn uses them.
These checks are not a firewall. The conversation agent is external to the guarantee.

The first test run gave `14 failed in 0.41s` for missing configuration behavior.
A late speaker result also caused `TimeoutError` at the transcript-return bound.
HA gives the transcript at `final` and reads speaker metadata with a different, bounded read.

The checks use these commands from the repository root:

| Command | Result |
| --- | --- |
| `uv run --locked ruff check --no-cache .` | `All checks passed!` |
| `PRIVATE_DENYLIST="$PRIVATE_DENYLIST" uv run pytest tests/test_modes.py tests/test_diagnostics.py tests/test_sensor.py tests/test_config_flow.py tests/test_repairs.py -q` | `63 passed` |
| `PRIVATE_DENYLIST="$PRIVATE_DENYLIST" task check` | `649 passed`, clean format and lint checks, mypy found no issues in 21 source files |
| `task package-check` | `PASS packaging (local check, not Marketplace or HACS-default acceptance)` |
| `task ha-minimum` | `649 passed` on Python 3.14.2 with HA 2026.10.1 |
| `task hassfest` | `Invalid integrations: 0` |
| ASD-STE100 checker on `STRUCTURE.md`, `docs/configuration.md`, `docs/privacy.md`, and this file | No findings |

The full check also gave `PASS integration bootstrap`, `PASS CI`, and `PASS public data matches=0`.
The worktree and Git history secret checks found no leaks. The history privacy check gave exit 0.

Ten temporary mutations each caused one test failure:

| Mutation | Result |
| --- | --- |
| Use a local destination for a missing `iot_class` | `1 failed` |
| Let a cloud fallback use an override | `1 failed` |
| Let a refused TTS fallback run at use time | `1 failed` |
| Turn transcript storage on by default | `1 failed` |
| Remove the PyPI publication requirement | `1 failed` |
| Use unchecked audio-format fields in diagnostics | `1 failed` |
| Identify proxy latency as measured | `1 failed` |
| Remove per-turn destination records | `1 failed` |
| Remove the speaker result | `1 failed` |
| Replace the fallback end-decision source | `1 failed` |

The mutation runner writes the initial source bytes in `finally` and checks their equality.
The source has none of these mutations.

The checks have these differences from a full server trial:

- Transcript storage tests use the pinned client, a fake server, and SQLite export. They do not test the server store implementation.
- The server `profile` stays `unknown` because the pinned health contract does not supply it.
- Piper replaces the earlier Kokoro example. The local voice is `en_US-lessac-medium` at 22050 Hz.
- The packaging file list and its explicit test include the new `sensor.py` module.
- STT fixtures include speaker metadata and the fallback `end_decision` event. A configuration fixture supplies empty options.
- The fallback end-decision source is `ha_audio_end`, with proxy quality. It is not a server-detector measurement.
- Test event listeners use `homeassistant.core.callback`. HA runs a listener without that mark in a different worker, so a test could read the event list before the listener added the event. For this cause, one full run gave `assert 0 == 1` in `test_unexpressible_tts_stream_is_refused[second_format_after_audio]`. A check for no event was also not reliable when the event came late.

No hardware trial, cloud request, commit, or publication is part of these checks.

## Server selection with local only

The client source pin is `f6ac2d61da342dc962db446d9e9d5fab8f80ee95`.
The health decoder supplies the server `local_only` state and the configured destination map.
With HA local only on, server `local_only=true` and destination `local` are necessary for each STT and TTS function.
Missing fields, missing parts, `false`, `null`, `cloud`, and `unknown` select the fallback.
With HA local only off, healthy server functions keep their usual selection.

Tests use the pinned client decoder and audio methods with a fake HTTP and WebSocket peer.
Each refusal keeps its cause in the turn record, without transcript or reply text.
Diagnostics show the last health fields, and missing fields show as `not_reported`.
Speaker health does not select STT or TTS. The speaker result keeps its use-time check.
An option change updates the ledger without a restart.

The full local test suite gave `703 passed`.
Seven temporary source changes each caused a named test failure:

- Remove the server local-only check.
- Accept a missing server local-only state.
- Remove the part destination check.
- Accept an `unknown` destination.
- Remove the RIFF magic check.
- Remove text-producer cancellation from cleanup.
- Include a fallback entity ID in diagnostics.

Each subprocess puts the initial file contents back in `finally`.
The privacy checks use `$PRIVATE_DENYLIST` without a published local path.
These checks do not include a hardware trial, cloud request, commit, or publication.
