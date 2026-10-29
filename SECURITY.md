# Vulnerability reports

## Status

The project is experimental and has no production release.
Use an isolated HA test instance, not a live household instance.

## Report a problem

Use GitHub private vulnerability reporting for this repository.
If private reporting is not available, use a private maintainer contact on the organization page.
Do not open a public issue with credentials, audio, transcripts, or personal data.
Give the version with the problem and synthetic steps that show it.
No response-time guarantee applies before the first release.

## Design limits

All device control must use the HA conversation agent and HA exposure rules.
The server must not receive an HA administrator credential.
The local-only setting controls assist-live destinations, not the selected conversation agent or other integrations.
It is not a network firewall.
See [the design](docs/design.md) for fallback and late-result limits.
