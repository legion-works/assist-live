# Recording consent

The project is experimental and has no release.
No volunteer recording is available at this time.
Get operator approval for the corpus source before collection or import.

## Volunteer process

1. Tell each volunteer about the recording, test playback, and public redistribution.
2. Get written consent for recording and redistribution before recording starts.
3. Use only `CC0-1.0` or `CC-BY-4.0` for the recording.
4. Record only generic test commands for virtual test entities.
5. Do not record names, addresses, household voice, or other personal data.
6. Give each speaker an anonymous identifier such as `speaker-01`.
7. Give each consent record an identifier such as `consent-01`.
8. Keep signed consent records and the identity mapping external to Git.
9. Check each recording and transcript for personal data before corpus import.
10. Record the permitted use and license in the public consent index.

The private record must identify the person who gives consent and the recordings with consent.
It must state the permitted uses and the selected license.
Tell the volunteer that an open license lets other users redistribute recordings.
Do not promise that a subsequent request can remove copies that other users hold.
Get legal advice if the volunteer cannot give consent or the license terms are unclear.

## Public index

`tests/data/consent-index.json` is an array of records with only four fields:

| Field | Content |
|---|---|
| `consent_id` | Anonymous consent identifier |
| `permitted_use` | Recording, redistribution, and test playback permission |
| `license` | `CC0-1.0` or `CC-BY-4.0` |
| `attestation` | Redacted statement of written consent for these uses |

Do not put signatures, legal identities, contact information, or private document locations in the index.
The checker rejects other fields.
A maintainer must examine the statement and the private record.
A statement alone does not show legal consent.

For `CC-BY-4.0`, agree on a public attribution that does not include a legal identity.
Put that attribution in each corpus record.

Use `source_uri=consent:CONSENT_ID` and pin the external bundle revision in `source_revision`.
Keep the bundle external to Git.
Import the approved bundle below `--audio-root` before a corpus check.

## Public datasets

The operator can give approval for a public dataset as an alternative to volunteer recordings.
Check its recording license and redistribution terms before import.
Use only `CC0-1.0` or `CC-BY-4.0`.
Do not use an NC or SA license.

Pin each source URL, revision, audio hash, and necessary attribution in the corpus records.
Public dataset records use `consent_id: null`.
They do not claim volunteer consent.
This repository contains imported dataset records, but no human audio.
