# Slice 01: Docker migration guide

## Goal and observable outcome

Existing Docker users can upgrade without guessing which settings remain valid,
which settings are newly required, or where optional installer web access now
lives.

## Scope

- Add one README migration section near the Docker quick start.
- Cover `docker run` and Compose with complete replacement configuration.
- Document validation, optional web migration, old-proxy disposition, and
  rollback preparation.

## Non-scope

- Runtime, Dockerfile, MQTT, inverter, or CI changes.
- Removing the authorized root key or actively cleaning the old inverter proxy.

## Dependencies and ordering

This single slice documents behavior already delivered by commits `3461da7`,
`03eae47`, and `8a90379`.

## Entry point and end-to-end behavior

- Explain that `/key/id_rsa`, MQTT variables, topics, and Home Assistant remain
  compatible.
- Explain that the key and `SSH_HOST_FINGERPRINT` are mandatory, the Docker host
  must reach inverter TCP/22, and inverter TCP/8000 is unused.
- Show how to preserve the previous image/config, obtain the fingerprint, pull
  the image, recreate a `docker run` container, or update/recreate a Compose
  service.
- Show validation through `docker logs` and clarify automatic reconnect output.
- Explain that the web gateway is disabled unless explicitly published and that
  old `<inverter>:8000` bookmarks move to authenticated
  `<docker-host>:8000`; writes remain separately opt-in.
- State that no inverter cleanup is required and `-t` is obsolete.

## Data, authorization, and permissions

No data model changes occur. The key must remain mode `0600` and be mounted
read-only. Optional web access requires Basic Auth; Basic Auth is suitable only
for the trusted LAN without an added TLS proxy.

## Validation and error handling

- Commands use implemented environment names, defaults, and mount paths.
- The Compose example parses successfully.
- Migration checks distinguish tunnel establishment, fingerprint mismatch,
  connection failure, and unchanged MQTT delivery.
- Rollback restores the prior container image/configuration, not inverter
  modifications.

## Implementation surfaces

Only `README.md` and these delivery plan artifacts change.

## Validation commands

- Validate the embedded Compose document with `docker compose config`.
- Check environment names against `Dockerfile` and `pika2mqtt.py`.
- Check internal links and run `git diff --check`.
- Inspect the final diff; executable tests are not required for this
  documentation-only slice.

## Acceptance criteria and commit boundary

The migration section is complete for both deployment styles, explicitly calls
out every breaking and unchanged interface, provides verification and rollback
steps, passes documentation validation, and is committed with only README and
plan artifacts.
