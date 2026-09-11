# Slice 03: Container configuration and migration documentation

## Goal and outcome

Container users can persist learned strings, configure discovery/health/MQTT
behavior, and safely migrate from the removed legacy topics.

## Scope

- Add environment/CLI configuration for MQTT port/client ID, discovery enable
  and prefix, inventory path/freeze, detail polling interval, and disconnect
  threshold.
- Declare `/data` for persistent inventory and update Docker examples without
  changing the read-only `/key/id_rsa` contract.
- Rewrite the MQTT documentation for JSON state, availability, device
  discovery, measurements, fault semantics, commissioning, freezing, reset,
  and the breaking removal of legacy topics and scaled battery values.
- Extend the existing migration section with dashboard/automation impact and a
  Compose example that mounts a dedicated data path.

## Non-scope

TLS, CI publishing changes, automatic deletion of user state, or any changes on
the inverter.

## Dependencies and behavior

Depends on slices 01 and 02. Learning defaults enabled and discovery defaults
enabled. Recommended commissioning learns all observed strings, then sets
`PV_INVENTORY_FREEZE=true`. If frozen without an inventory, startup continues
with a prominent warning and zero expected strings. Reset is an explicit user
operation against only the configured inventory file while the container is
stopped. The SSH key remains a separate read-only mount and is never included
in `/data`.

## Validation

- Positive/negative parser tests cover every new environment value and invalid
  ranges/booleans/partial credentials.
- Review README examples against the actual Dockerfile and CLI.
- Run the complete unit suite, `python3 -m py_compile` for runtime modules, and
  `docker build . --file Dockerfile --tag pika2mqtt:test` when available.

## Acceptance and commit boundary

Fresh and migrating Docker users have complete, accurate configuration and
rollback guidance. Commit Dockerfile, README, parser tests, and this slice plan
as the final slice.
