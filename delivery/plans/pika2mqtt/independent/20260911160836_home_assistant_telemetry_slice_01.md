# Slice 01: Telemetry and PV inventory

## Goal and outcome

Build a deterministic telemetry snapshot from the installer API and retain the
identity/health of every observed PV Link across restarts. Callers can inspect
normalized system, inverter, grid, battery, and per-string data without MQTT.

## Scope

- Parse `/devices` and the UI-defined `common`, `REbus_status`,
  `inverter_status`, `REbus_exp`, `inverter`, `battery`, `pvlink_status`, and
  `pvrss_telemetry` models.
- Poll devices every 15 seconds and detail models every 60 seconds, applying
  per-route exponential backoff capped at 15 minutes after repeated failures.
- Decode the full REbus state table needed by observed devices and preserve
  unknown codes in numeric/hex form.
- Learn PV Link RCPNs without an upper limit into an atomic, versioned JSON
  inventory. A frozen inventory never learns unknown RCPNs; corrupt inventory
  is fatal rather than overwritten.
- Derive per-string connectivity after 120 seconds, separate fault state, and
  aggregate learned/connected/disconnected/faulted counts.

## Non-scope

MQTT connection behavior, Home Assistant discovery, Docker configuration, and
any inverter or SSH-key modification.

## Dependencies and behavior

This is the first slice. `/devices` is authoritative for presence, power, and
`lastheard`. Model HTTP errors retain prior detail values and expose endpoint
health but do not disconnect a string. `LOW_SUN`, disabled, and transitional
states are descriptive, not connectivity failures. Error-range REbus states,
nonzero PV error words, PVRSS lockout, and explicit self-test failures produce
the independent fault signal. Ignored serials remain in inventory but are
excluded from snapshots and aggregates.

Inventory writes use a same-directory temporary file plus atomic replace. The
default path is `/data/pv_inventory.json`; parent directories may be created.
Authorization is not applicable: this is local state and read-only HTTP API
access. Existing SSH transport gates all requests.

## Implementation surfaces

Add focused telemetry/inventory modules and fixtures/tests; adapt the collector
entry point only enough to construct and poll them. Do not alter the SSH
transport module or private-key handling.

## Validation

- Positive: parse all model fixtures, normalize units/directions, decode known
  states, learn multiple strings, reload inventory, and transition stale
  strings at exactly 120 seconds.
- Negative: malformed inventory, malformed/partial payloads, unknown states,
  400/500/timeouts, frozen unknown strings, and detail-route failures without
  false disconnection.
- Run `python3 -m unittest tests.test_telemetry -v` and related collector tests.

## Acceptance and commit boundary

Telemetry snapshots and inventory behavior pass focused tests independently of
MQTT. Commit the plan, modules, fixtures, tests, and minimal collector wiring as
one slice; do not include discovery or documentation migration work.
