# Slice 01: Resilient SSH transport

## Goal and observable outcome

The collector reaches the inverter's localhost installer API exclusively over
a supervised SSH local forward. Transient disconnects, inverter reboots, and
hung transport requests are detected, logged to container stdout/stderr, and
recovered without restarting the container.

## Scope

- Add host-fingerprint validation and a loopback-only SSH tunnel supervisor.
- Route existing collector HTTP requests through the tunnel.
- Add configuration validation, graceful shutdown, recovery logging, and
  positive/negative automated tests.
- Refactor the executable entry point enough to allow isolated testing.

## Non-scope

- Web access from the LAN.
- MQTT topic or calculation corrections.
- On-device installation or firewall changes.

## Dependencies and ordering

This is the first slice. It requires the existing private key but must not read,
copy, stage, or modify the repository's live `pika-rsa` file.

## End-to-end behavior and state

- Validate the private-key path and SHA-256 fingerprint syntax.
- Discover the ED25519 server key using `ssh-keyscan`, calculate its fingerprint
  using `ssh-keygen`, and write only the verified public key to an ephemeral
  known-hosts file.
- Start `ssh -N` with strict host checking, a loopback forward, batch mode,
  forward-failure detection, a ten-second connect timeout, fifteen-second
  keepalives, and a three-keepalive failure limit.
- Track disconnected, connecting, connected, stopping, and fatal states.
- Retry transient discovery and SSH failures after 1, 2, 4, 8, 16, 32, then at
  most 60 seconds, applying bounded jitter. Reset backoff after stable or
  successful API traffic.
- Count transport failures and recycle the active tunnel after three
  consecutive failures. Application HTTP statuses do not count.
- Gate requests on tunnel availability and return quickly while disconnected.
- On SIGTERM/SIGINT, stop requests, terminate and reap SSH, and exit cleanly.

## Authorization and security

Fingerprint mismatch is fatal and never falls back to insecure host checking.
Logs must not include private-key contents or credentials.

## Validation and errors

- Missing key, malformed fingerprint, or missing SSH tools fails startup.
- Offline hosts and authentication/connection errors retry with backoff.
- Fingerprint mismatch stops the supervisor and surfaces a fatal error.
- SSH stderr is drained and summarized without log flooding.

## Implementation surfaces

Expected changes include a transport module, the pika2mqtt entry point, and a
new unittest suite with sanitized live-response fixtures.

## Tests and commands

- Positive: fingerprint match, tunnel establishment, success reset, collector
  request through the local endpoint, graceful stop.
- Negative: malformed/mismatched fingerprint, scan failure, SSH exit, three
  transport failures, backoff growth/cap, and unavailable tunnel.
- Run: `python3 -m unittest discover -s tests -v`
- Run: `python3 -m py_compile pika2mqtt.py pika_transport.py`

## Acceptance criteria and commit boundary

The collector no longer invokes `keep_running.sh` or requests the inverter's
port 8000, tunnel recovery is observable in logs, focused tests pass, and only
slice-owned code, tests, fixtures, and plan artifacts are committed.
