# Verification record

## Environment source

Date: 2026-10-24 (UTC).
The source is [PyPI metadata for HA 2026.10.3](https://pypi.org/pypi/homeassistant/2026.10.3/json), field `info.requires_python`.
The fetched value is `>=3.14.2`.
The project uses `requires-python = ">=3.14.2"` and `.python-version` value `3.14.2`.
No HA or unpublished client package is a bootstrap dependency.

The [HACS manifest documentation](https://www.hacs.xyz/docs/publish/start/#hacsjson) lists the `name` and `homeassistant` keys.
It does not list `render_readme`, so this bootstrap does not include that key.
The package is not in the repository root, so it does not set `content_in_root`.

## Test-first package boundary

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
The repository has 0 commits and no remote. No publication occurred.

## Mandatory checks

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

Here, `STE_CHECK` is the external `write-plain/scripts/ste_check.py` checker.

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

## Diagnostic environment limit

Date: 2026-10-24 (UTC).
`aft_inspect` examined this repository with its `scope` set to the repository root.
Its snapshot was `FRESH`, with authoritative diagnostics for 6 of 6 Python files.
Its first full report had 4 errors and 0 warnings:

```text
config_flow.py:3: Import "homeassistant" could not be resolved [Pyright]
config_flow.py:4: Import "homeassistant.data_entry_flow" could not be resolved [Pyright]
config_flow.py:7: Incorrect keyword arguments for __init_subclass__ method [Pyright]
config_flow.py:7: No parameter named "domain" [Pyright]
```

The project environment intentionally contains pytest only, not HA.
The task owner deferred these environment diagnostics to B04, which adds HA type tooling.
This is `not a passing type check`.

After the stock HA correction below, the last snapshot has 3 errors and 0 warnings:

```text
config_flow.py:3: Import "homeassistant" could not be resolved [Pyright]
config_flow.py:6: Incorrect keyword arguments for __init_subclass__ method [Pyright]
config_flow.py:6: No parameter named "domain" [Pyright]
```

## Stock HA import and type checks

Date: 2026-10-24 (UTC).
The task owner approved temporary HA and mypy environments with no project dependency change.
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

The stock HA check does not replace B04's integrated type tooling and HA runtime tests.

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
The integration has no gitleaks configuration or exemptions.

## Checks not part of bootstrap

The HA runtime harness, integrated type checks, and HACS validation belong to subsequent work.
The repair round below adds a stock hassfest check without project dependency changes.
The bootstrap config flow aborts and makes no config entry.
No runtime, hardware, cloud, release, or Marketplace acceptance result follows from these checks.

## `Fix round 1`

Date: 2026-10-24 (UTC).
The review found errors in manifest order, config steps, IPv4 punctuation, private paths, and nested-repository detection.
The repairs keep the package experimental and keep its setup-abort behavior.

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

### Stock hassfest

The validator source came from the `home-assistant/core` tag `2026.10.3` archive.
`HASSFEST_ROOT` below is the temporary directory with that tag's `script` package.
The method uses the stock validator and stock HA in a temporary environment, as in the review.
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

### Other gates

The different publishable-file check used literal denylist searches and a different address-check implementation.
It gave:

```text
PASS own Git root
files=28 denylist=0 private_path=0 rfc1918=0
PASS independent publishable-file self-scan
```

The scoped AFT snapshot is `FRESH`, with authoritative reports for 6 of 6 Python files.
It has the same 3 HA-environment errors and 0 warnings recorded above.
The task-owner-approved B04 deferral applies. This is `not a passing type check`.
No Python signature changed in this repair round.

The standard STE checker ran on each of the 12 public Markdown files and the implementation report.
Each file has 0 findings and exit code 0.
The command for each file was `python3 "$STE_CHECK" "$FILE"`, as specified above.

## `Fix round 2`

Date: 2026-10-24 (UTC).
The Unix-path expression rejects local user paths but accepts the two public URL test inputs from the review.
The four no-trailing-separator tests continue to reject private paths.

The project selects HA `2026.10.1` as the HACS minimum, in agreement with the planned B04 matrix.
The bootstrap checker rejects the former `2026.10.0` minimum.
The design marks the HA minimum, `integration_type`, and `iot_class` as project decisions, not frozen-spec requirements.
The package test resolves its root from `Path(__file__).resolve().parents[1]` and keeps all plan assertions.

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

### Requested gates

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

The scoped AFT result is `FRESH` and has the same 3 HA-environment errors and 0 warnings recorded above.
The prior B04 deferral applies. This is `not a passing type check`.
