# Slice 04: Final recovery and frozen-inventory hardening

## Goal and outcome

Close two acceptance gaps found during final reconciliation: MQTT TCP connection
failures are visible in container logs, and PV Links observed while inventory
learning is frozen remain visible in the system state and solar total without
silently becoming monitored children.

## Scope and behavior

- Register the Paho initial-connect-failure callback, clear connection state,
  and log that automatic retry remains active.
- Add untracked PV Link count/serials to the system JSON and a diagnostic Home
  Assistant count entity.
- Include every currently visible, non-ignored PV Link in system solar power,
  while connectivity/fault aggregates continue to cover only learned strings.

This does not change learning, discovery identity, SSH/key handling, or the
meaning of freeze. Authorization is not applicable.

## Validation and commit boundary

- Verify initial MQTT failures log and never release CONNACK gating.
- Verify a frozen unknown PV Link contributes power and appears in the untracked
  diagnostics, but receives no child discovery or health obligation.
- Run focused MQTT/telemetry tests, the complete suite, syntax checks, and the
  container build. Commit only this plan, runtime corrections, and tests.
