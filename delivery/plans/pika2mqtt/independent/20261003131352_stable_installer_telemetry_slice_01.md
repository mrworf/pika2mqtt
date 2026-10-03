# Slice 01: Stabilize slow detail reads and PV state

Parent commit: `752702ed0665cbda332e7b864dab4f05027e441f`

## Goal and observable outcome

Slow but valid installer model responses no longer become false failures, and
Home Assistant retains reliable per-string status and fault monitoring from
the controller directory when optional PV detail routes are rejected by the
firmware.

## Scope and non-scope

- Add a configurable detail-request timeout with a 20-second default while
  retaining the existing primary and control request timeout.
- Prioritize the controller directory among due background detail work.
- Keep exponential detail retries with an approximately 15-minute maximum
  cadence while preserving jitter at the cap.
- Record endpoint failure classification, request duration, consecutive
  failures, and next retry time; suppress identical repeated warnings while
  logging recovery and slow successful calls.
- Map a fresh, identity-verified controller-directory entry into each PV
  Link's core REbus state and detailed-fault coverage.
- Use directory-backed availability for normalized PV status and fault
  entities, and add a disabled-by-default detailed-fault-data-unavailable
  diagnostic.
- Document configuration and firmware behavior.
- Do not change primary polling cadence, control semantics, the SSH key, the
  inverter filesystem, operating mode, or PV enable state.

## Dependencies and end-to-end behavior

The primary loop continues reading `/devices` independently. The background
worker chooses overdue controller-directory work before other overdue routes,
performs only one client-side detail request at a time, and waits up to the
configured detail timeout for the appliance's Modbus-backed response.

A PV Link is matched to exactly one current controller repeating block using
its manufacturer, device type, device ID, and Modbus unit ID. Fresh directory
`St`, `P`, `V`, `I`, `T`, `Rb`, `Ena`, and `UpdtTm` values provide core state;
unsafe energy counters are not promoted. Fresh optional `pvlink_status` and
`pvrss_telemetry` continue to add detailed faults. Core Fault is available when
the directory is fresh, while a separate coverage diagnostic reports whether
both optional detail sources are fresh.

## Validation and recovery behavior

- Detail timeouts below one second are rejected by argument validation.
- HTTP errors, timeouts, malformed payloads, and slow successes receive
  distinct health metadata.
- Identical failures warn once per unavailable episode; changed failure types
  warn again, and the first success logs recovery.
- Capped retries add bounded positive jitter so routes do not reconverge.
- Missing, stale, malformed, or ambiguous directory data cannot synthesize a
  healthy core fault state.
- Optional detail recovery automatically restores detailed coverage.
- Authorization is not applicable because this slice changes read-only
  telemetry only; all existing write controls remain separately gated.

## Implementation surfaces and tests

- Update `telemetry.py` for timeout separation, scheduling, health metadata,
  directory-backed PV normalization, fault semantics, and logging.
- Update `pika2mqtt.py` and `Dockerfile` for configuration.
- Update `mqtt_bridge.py` for directory-backed availability and the coverage
  diagnostic.
- Update telemetry, configuration, and MQTT tests with positive and negative
  cases; update `README.md` for operations and migration guidance.

Required validation:

- `python -m unittest tests.test_telemetry tests.test_configuration tests.test_mqtt_bridge -v`
- `python -m py_compile telemetry.py mqtt_bridge.py pika2mqtt.py`
- `python -m unittest discover -s tests -v`
- `docker build -t pika2mqtt:stable-installer-telemetry-test .`
- `git diff --check`

## Acceptance and commit boundary

All approved behavior, tests, documentation, and plan artifacts form one
independently revertible commit. Automated tests use fakes only and perform no
live installer writes.

## Completion evidence

- Focused telemetry, configuration, and MQTT suite: 75 tests passed.
- Full repository suite: 96 tests passed, including loopback gateway tests.
- `telemetry.py`, `mqtt_bridge.py`, and `pika2mqtt.py` compile successfully.
- Docker image `pika2mqtt:stable-installer-telemetry-test` builds successfully.
- The built image exposes `--detail-request-timeout` and missing arguments exit
  with the expected status 2.
- `git diff --check` passes.
- All validation used fixtures or read-only behavior; no live installer write,
  operating-mode change, or PV Link state change was performed.
