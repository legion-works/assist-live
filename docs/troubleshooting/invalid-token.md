# Invalid access token

The server rejected the assist-live access token. Check for an incorrect or expired token.

During installation, record the correct integration token in the setup step.
After installation, open **Settings > System > Repairs** and do the [reauthentication steps](../setup.md#reauthentication).
The configured fallback engines answer turns while HA waits for the replacement token.

If HA does not accept the replacement token:

- Make sure that the token is for the server URL in this entry.
- Make sure that the server gives that token access to all satellite keys in this entry.
- Make sure that no different entry on the same server uses that token.

If HA cannot read the server bindings, check the server URL and server status. A connection failure alone does not start reauthentication.
