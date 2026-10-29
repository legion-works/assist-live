# Third-party notices

## Dependencies

This repository contains no third-party model, voice, audio, or copied runtime source.
The project uses the full Apache-2.0 license in `LICENSE`.

The development environment uses pytest (MIT) and its dependencies from PyPI.
See `uv.lock` for package versions and hashes.
Git and gitleaks are external checker tools, not vendored code.

## Release gate

Record the license and necessary attribution of each library, model, and voice before inclusion or download in a release.
A license does not stop component selection. Each third-party component keeps its own license and requirements for supply.
CC-BY-4.0 attribution for the Parakeet provider is necessary in the server repository.
The Piper runtime has the GPL-3.0-or-later license, not the project's Apache-2.0 license.
The server repository records model, voice, and runtime licenses and attribution.
