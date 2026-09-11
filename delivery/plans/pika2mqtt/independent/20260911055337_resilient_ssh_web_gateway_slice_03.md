# Slice 03: Container packaging and operator documentation

## Goal and observable outcome

The published container contains all tunnel dependencies, starts with the new
configuration, documents safe read-only and write-enabled deployments, and no
longer ships or recommends the on-device proxy/firewall hack.

## Scope

- Install the OpenSSH client tooling required at runtime.
- Wire environment-variable defaults and document CLI equivalents.
- Retire obsolete on-device helper scripts and rewrite setup/troubleshooting.
- Ignore the repository-local live key and run final validation.

## Non-scope

- Inverter firmware changes, TLS, MQTT behavior changes, or unrelated CI
  modernization.

## Dependencies and ordering

Requires slices 01 and 02 so documentation and image behavior describe tested
interfaces.

## End-to-end behavior and state

- Support `SSH_HOST_FINGERPRINT`, `SSH_PORT`, `WEB_ENABLED`,
  `WEB_WRITE_ENABLED`, `WEB_LISTEN`, `WEB_PORT`, `WEB_USERNAME`,
  `WEB_PASSWORD_FILE`, and `WEB_PASSWORD` with documented defaults.
- Document host fingerprint discovery, read-only key mounting, Basic Auth secret
  mounting, explicit Docker `-p 8000:8000`, write opt-in risk, reconnect logs,
  health troubleshooting, and migration from the old on-device proxy.
- Remove executable proxy/firewall/keep-running helpers and obsolete advice.
- Add `/pika-rsa` to the root ignore file without staging the key.

## Authorization and security

Documentation must state that port publication is LAN-only, Basic Auth is not
TLS, write access can alter inverter configuration, and the root key must be
mounted read-only with restrictive permissions.

## Validation and errors

The image build must contain `ssh`, `ssh-keyscan`, and `ssh-keygen`. Startup
validation messages must identify missing variables without printing secrets.

## Implementation surfaces

Expected changes include the Dockerfile, README/install documentation,
`.gitignore`, CI validation, and deletion of obsolete helper files.

## Tests and commands

- Run: `python3 -m unittest discover -s tests -v`
- Run: `python3 -m py_compile pika2mqtt.py pika_transport.py web_gateway.py`
- Run: `bash -n` for any remaining shell scripts.
- Run: `docker build . --file Dockerfile --tag pika2mqtt:test` when Docker is
  available.
- Verify: `git check-ignore pika-rsa` and inspect staged content for key data.

## Acceptance criteria and commit boundary

The image builds with required SSH tools, documentation gives complete operator
commands, obsolete on-device mechanisms are gone, all tests pass, `pika-rsa`
is ignored and uncommitted, and the final slice is committed independently.
