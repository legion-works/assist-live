# Marketplace release and validation status

Date: 2026-10-24 (UTC).
Status: `pending_release` for HA `2026.11`.
The project is experimental and has no release.
This repository has no Marketplace acceptance result.
HACS validation success is not Marketplace acceptance.

## Release observations

The explicit live probe read these official sources on 2026-10-24 (UTC):

| Source | URL | Observed result |
| --- | --- | --- |
| PyPI release list | <https://pypi.org/pypi/homeassistant/json> | Latest stable `2026.10.4`. No stable `2026.11.x` with release files. |
| Core GitHub releases | <https://api.github.com/repos/home-assistant/core/releases?per_page=100> | Latest stable `2026.10.4`. No stable `2026.11.x` in the latest 100 releases. |
| November release-note URL | <https://www.home-assistant.io/blog/2026/11/04/release-202611/> | HTTP 404. This URL is a check, not proof of a scheduled release date. |

The GitHub source does not include drafts or prereleases.
The PyPI source does not include versions without release files or versions with beta, release-candidate, or development suffixes.
A missing blog page alone does not prove that HA has no release.
The three observations agree on this date.
The stored status is a dated observation, not a current network check.

## Official rule

Source: [repository structure](https://contributors.home-assistant.io/marketplace/reference/repository-structure/), accessed 2026-10-24 (UTC).
The section "Types the Marketplace does not install" states:

> "HACS also handles AppDaemon apps (`apps/`) and Python scripts (`python_scripts/`), and the catalog has lists for them. The Marketplace does not install either. Starting with 2026.11, Home Assistant also blocks the HACS [custom integration](https://contributors.home-assistant.io/glossary/custom-integration/), because the Marketplace replaces it. So on 2026.11 and later, neither installs them."

> "The Marketplace also refuses repositories with [apps](https://contributors.home-assistant.io/glossary/app/) for [Home Assistant OS](https://contributors.home-assistant.io/glossary/home-assistant-os/). Those are added as an app repository under **Settings** > **Apps** instead: see [create and share a repository](https://contributors.home-assistant.io/apps/guides/repository/)."

For HA `2026.11` or newer, the Marketplace is the installation method, not the HACS custom integration.
HA refuses the HACS custom integration itself, not all custom integrations.
HA OS apps use **Settings > Apps**, not the Marketplace.
AppDaemon apps and Python scripts are different types that the Marketplace does not install.

Source: [validation checks](https://contributors.home-assistant.io/marketplace/reference/validation/), accessed 2026-10-24 (UTC).
The section "What the Marketplace checks" states:

> The Marketplace checks before an install, and again after it wrote the files.

The page points to `homeassistant/components/marketplace/repositories/` in core.
`hacs/action` and hassfest are different checks.
These checks do not supply the necessary Marketplace install result.

## Pinned development source

The live probe read core `dev` commit `7c3a98ae6f4454d65500438b2fb503363fcf490a`.
The commit date is `2026-10-24T21:19:01Z`.
Access date: 2026-10-24 (UTC).
This is development source, not a released validator.

Source: [integration repository validator](https://github.com/home-assistant/core/blob/7c3a98ae6f4454d65500438b2fb503363fcf490a/homeassistant/components/marketplace/repositories/integration.py#L313-L321).
The related check is:

```python
            name = get_first_directory_in_directory(self.tree, "custom_components")
            if name is None:
                if (
                    "repository.json" in self.treefiles
                    or "repository.yaml" in self.treefiles
                    or "repository.yml" in self.treefiles
                ):
                    raise AppRepositoryError(self.data.full_name)
```

This branch refuses an app repository when it has no integration directory.
It does not prove refusal of a mixed repository with an integration and an app.
The release trial must check the rule against the released source.
The three-repository layout stays unchanged.
The [public design record](../design.md) states:

> It must check HA Marketplace rules on a released HA version before an acceptance claim.

## Check-again condition and procedure

This procedure is not a validation result.
Start it when PyPI publishes the first stable `homeassistant` `2026.11.x` with release files.

Make sure that core GitHub releases has the same tag.
Read the official release notes.

Do not use a beta, release candidate, `hacs/action`, or a `dev` checkout as the released validator.

1. Run the explicit network probe from the repository root:

   ```sh
   uv run python tools/check_marketplace.py --minimum 2026.11 --report docs/research/q3-marketplace.md --live --record-status
   ```

   For GitHub API access, set `GITHUB_TOKEN` or `GH_TOKEN` in the environment before the probe.
   A shared unauthenticated rate limit can stop the probe with `github_rate_limit`.
   The probe sends the token only to the HTTPS GitHub API and does not print it.

   After a stable release, the pending record must give a failure until it has released validation records.
   This command prints source observations. It does not install a repository or change the report.

2. Re-read `homeassistant/components/marketplace/repositories/integration.py` at that release tag.
   Use `https://github.com/home-assistant/core/blob/<release-tag>/homeassistant/components/marketplace/repositories/integration.py`.
   Resolve the tag to a commit with `gh api repos/home-assistant/core/git/ref/tags/<tag>`. If `object.type` is `tag`, read `gh api repos/home-assistant/core/git/tags/<object.sha>` and use its `object.sha` commit. Record the tag and resolved commit.
   Re-read the official rule and validation documentation above.
   Record the tag, commit, access date, and the complete app-refusal condition.
   Set `validator.tag` to the stable release version and use that tag in `validator.source_url`.
   A development-commit URL or a beta/RC tag cannot supply a released validation result.

3. Make the local app-only fixture:

   ```sh
   uv run pytest tests/test_marketplace.py::test_local_app_fixture_is_not_marketplace_acceptance -q
   ```

   The test writes `app-repository/repository.yaml` and `app-repository/probe_app/config.yaml` below the temporary test directory.
   The fixture has no `custom_components` directory.
   The test also makes `mixed-repository` with the app descriptor and a copy of `custom_components`.
   It has synthetic metadata only. Do not publish it or build an app image.

   This test makes the fixture. It does not run the Marketplace validator.

4. With operator approval, start a different HA `2026.11.x` test container on loopback.
   Do not use the HA `2026.10.3` instance or a household instance.
   Use **Settings > Marketplace**, the documented install path, for the integration repository.
   Necessary result: acceptance of the integration repository.
   Apply the same released install-time validator to the local fixture through an isolated repository-source test adapter.
   The adapter must supply the fixture tree where the Marketplace usually reads repository files.

   Do not publish a fixture repository for the public-GitHub input requirement.
   Necessary result: `AppRepositoryError` and refusal before the Marketplace installs files.
   Use the same adapter for the mixed fixture.
   Record its actual result without a necessary accept/refuse outcome.
   The documented rule and the development branch are different on this condition.
   Keep the three-repository layout for each possible result.

   An adapter is necessary because the documented UI accepts public GitHub repositories, not local directories.
   Its implementation and all three released install trials stay pending.

5. Record all three outcomes in this report.

   Include the release version, validator, and dated source logs.
   `PASS` from HACS does not count as a Marketplace outcome.
   An acceptance claim must have `PASS Q3 accepted` from the isolated gate:

   ```sh
   uv run python tools/check_marketplace.py --minimum 2026.11 --report docs/research/q3-marketplace.md --require-accepted
   ```

   Only `PASS Q3 accepted` from this gate lets the project claim acceptance.
   Today it gives `FAIL Q3 pending_release: HA 2026.11 has no recorded stable release` and exit 1.

## Recorded source data

The offline gate reads this record. It does not use the network.
The `sha256` values identify the HTTP response bodies read by the live probe.
Null validation fields mean no install trial, not acceptance.

```json
{
  "minimum": "2026.11",
  "checked_at": "2026-10-24",
  "status": "pending_release",
  "release_version": null,
  "sources": {
    "pypi": {
      "url": "https://pypi.org/pypi/homeassistant/json",
      "checked_at": "2026-10-24",
      "http_status": 200,
      "sha256": "55e354feed5ad560892b3e76bc2b84718fd852ef9862771ff203204d49a97112",
      "latest_stable": "2026.10.4",
      "target_releases": []
    },
    "github": {
      "url": "https://api.github.com/repos/home-assistant/core/releases?per_page=100",
      "checked_at": "2026-10-24",
      "http_status": 200,
      "sha256": "11e7c2dfb6a799e7e5e7da42f80bd2b4c282e80ed25116855df30c057acf1a3c",
      "latest_stable": "2026.10.4",
      "target_releases": []
    },
    "release_notes": {
      "url": "https://www.home-assistant.io/blog/2026/11/04/release-202611/",
      "checked_at": "2026-10-24",
      "http_status": 404,
      "sha256": "6ed3bd27c4df160a2c85c0dc84862d84b082f71e0f15e88cd75fa2875ef7498b"
    }
  },
  "validation": {
    "integration": null,
    "app_fixture": null,
    "mixed_fixture": null
  },
  "rule": {
    "url": "https://contributors.home-assistant.io/marketplace/reference/repository-structure/",
    "checked_at": "2026-10-24",
    "state": "documented",
    "quote": "The Marketplace also refuses repositories with apps for Home Assistant OS."
  },
  "validator": {
    "documentation": "https://contributors.home-assistant.io/marketplace/reference/validation/",
    "checked_at": "2026-10-24",
    "commit": "7c3a98ae6f4454d65500438b2fb503363fcf490a",
    "tag": "dev",
    "commit_date": "2026-10-24T21:19:01Z",
    "source_url": "https://github.com/home-assistant/core/blob/7c3a98ae6f4454d65500438b2fb503363fcf490a/homeassistant/components/marketplace/repositories/integration.py",
    "line": 317,
    "quote": "            name = get_first_directory_in_directory(self.tree, \"custom_components\")\n            if name is None:\n                if (\n                    \"repository.json\" in self.treefiles\n                    or \"repository.yaml\" in self.treefiles\n                    or \"repository.yml\" in self.treefiles\n                ):\n                    raise AppRepositoryError(self.data.full_name)\n                raise MarketplaceError(\n                    translation_domain=DOMAIN,\n                    translation_key=\"structure_not_compliant\",\n                    translation_placeholders={\n                        \"repository\": self.data.full_name,\n                        \"version\": str(ref_version(self.ref)),\n                    },\n                )"
  }
}
```
