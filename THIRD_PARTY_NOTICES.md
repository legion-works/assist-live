# Third-party notices

## Bootstrap

This repository contains no third-party model, voice, audio, or copied runtime source.
The project uses the full Apache-2.0 license in `LICENSE`.

The development environment uses pytest (MIT) and its dependencies from PyPI.
See `uv.lock` for package versions and hashes.
Git and gitleaks are external checker tools, not vendored code.

## Release gate

Record the license and necessary attribution of each library, model, and voice before inclusion or download in a release.
A missing license or a non-commercial license stops the release.
CC-BY-4.0 attribution for the planned Parakeet provider is necessary in the server repository.
The Apache-2.0 server must not include the GPL-3.0 Piper runtime.
No model or voice license approval follows from this bootstrap.
