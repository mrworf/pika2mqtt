# Slice 01: Runtime diagnostics and reference

Goal: firmware-correct register interpretation in MQTT, container logs, and an
accessible reference. Depends only on existing SSH, collector and web gateway.

Scope: background XML retrieval/parser, explicit symbol policy, fresh decoded
observations, MQTT summaries/flags, atomic reference and discovery-manifest writes,
authenticated local reference routes, documentation and tests. Non-scope: new
inverter HTTP endpoints, control mutations, changing polling intervals.

Entry: application startup constructs loader and policy, starts independent worker,
and connects it to collector and gateway. Only fingerprint-verified SSH reads are
authorized. Web routes inherit existing gateway authentication and remain read-only.

State: no definitions -> loading -> valid snapshot; connection loss preserves valid
definitions, firmware changes invalidate them until reloaded; failed reload retries
with bounded backoff. Fresh register observations drive state/log transitions; stale
or ambiguous values are unavailable, never cleared. Discovery persists prior dynamic
components to remove obsolete ones after restart.

Surfaces: new register_definitions.py/register_policy.json; transport, telemetry,
MQTT, CLI and gateway integration; Docker configuration, README and focused tests.
Policy overrides are complete documents and invalid policy is a startup error.

Positive tests: firmware-dependent enum values, sparse bits, repeat records, reference
rendering, HA attributes/flags and errors, changes/recovery logging. Negative tests:
unknown/reserved bits, conflicts/malformed XML, missing/stale source, SSH/load failure,
invalid policy, persistence failure, unauthorized/writing reference requests.

Validation: python -m unittest discover -s tests -v; py_compile; git diff --check;
Docker build and CLI smoke. No live writes. Acceptance: all planned surfaces work
from one validated definition snapshot; missing definitions do not interrupt polling;
existing control and battery measurements remain intact.

Commit boundary: feature, tests, README and these plans together after validation.
Commit provenance: git log -- delivery/plans/pika2mqtt/independent/20261003_firmware_diagnostics_slice_01.md.

## Delivered and validated

Completed runtime XML retrieval, decoding/policy, MQTT diagnostics and persisted
discovery cleanup, change logs, atomic reference writes and authenticated HTML/MD
routes. Existing control behavior is unchanged. Missing module observations retain
unavailable register placeholders and generated entities rather than removing them.

Validation: 119 unittest cases passed (loopback suite run with required network
permission), Python compilation and diff whitespace checks passed. Docker image
pika2mqtt:firmware-diagnostics-test built; bundled policy, CLI help and missing-argument
exit-code checks passed inside it.

Read-only live validation loaded 132 firmware XML files and 287 register definitions;
three distinct ambiguous/conflicting registers were safely identified. Current live
St=2080 decoded GRID_CONNECTED, Ev=33536 decoded SYSMODE_CHANGE, RB=7 decoded the
three enable flags. HTML and Markdown reference generation succeeded. No inverter
control writes or production MQTT publication occurred.
