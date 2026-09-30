# Slice 01: Write and confirm the authoritative operating mode

Parent commit: `a35889fc26852a540ca1d30cb714e03dd2f01719`

## Goal and observable outcome

When the opt-in Home Assistant selector requests Grid Tie, Self Supply, Clean
Backup, or Priority Backup, pika2mqtt changes the persistent controller mode
through writable model 64200 and reports success only after both controller
and inverter status telemetry confirm it.

## Scope and non-scope

- Resolve the current LCM/system controller from `/devices`, require its
  installer API module ID to be 1, and retain the current inverter module ID
  for status confirmation.
- POST once to `/device/1/model/REbus_dir`; never POST to `inverter_status`.
- Confirm immediately, then at five-second intervals through the 30-second
  deadline using both `REbus_dir` and the inverter's `inverter_status`.
- Preserve the existing command topic, Home Assistant selector, allowed modes,
  default-off flag, ACL boundary, and command coalescing behavior.
- Do not retry the write, add optimistic state, expose additional modes, change
  the SSH key or inverter filesystem, or issue a live mode-changing request.

## Dependencies and end-to-end behavior

The MQTT callback continues to validate an exact option label and queues its
numeric code without blocking MQTT. The collector calls the telemetry write
path. That path requires a current inverter and a current `lcm` entry with
`modID` 1, posts `SysMd=<code>` once to the controller route, and serializes
confirmation reads with the detail collector.

Each confirmation attempt reads the controller model and inverter status. Both
must contain a finite integral `SysMd` equal to the requested code in the same
attempt. The first attempt is immediate; unsuccessful attempts wait five
seconds before retrying, with a final attempt at the 30-second boundary. A
confirmed inverter status response replaces the cached `inverter_status`
model, and only that snapshot is published.

Authorization remains the existing MQTT broker credentials and ACLs. The
feature remains disabled unless `OPERATING_MODE_CONTROL_ENABLED=true`.

## Failure and recovery behavior

- Missing or ambiguous controller/inverter mappings fail before the POST.
- A POST error fails immediately and is never retried automatically.
- Readback transport errors, malformed payloads, and mismatched values remain
  eligible for later confirmation attempts until the deadline.
- Timeout logs the requested code plus the last observed controller and status
  values, preserves the previous cached status, and publishes no synthetic
  state.
- A newer command received during confirmation remains queued and runs after
  the current command finishes; normal MQTT networking continues throughout.
- Regular polling remains authoritative after either success or failure and
  will expose any later external mode change.

## Implementation surfaces and tests

- Update `telemetry.py` for controller resolution, authoritative routing,
  bounded confirmation timing, and test-injectable waiting.
- Update `tests/test_telemetry.py` fixtures and cases for immediate/delayed
  confirmation, five-second cadence, exact routes, timeout, malformed and
  failing reads, and missing/wrong device mappings.
- Update `README.md` with corrected write semantics, confirmation behavior,
  affected-release warning, and owner-only manual validation.
- Preserve configuration, MQTT discovery, and orchestration tests unless a
  regression assertion needs to be strengthened; no public interface changes
  are expected in `mqtt_bridge.py` or `pika2mqtt.py`.

Required validation:

- `python -m unittest tests.test_telemetry tests.test_configuration tests.test_mqtt_bridge -v`
- `python -m py_compile telemetry.py mqtt_bridge.py pika2mqtt.py`
- `python -m unittest discover -s tests -v`
- CI missing-argument validation with installed dependencies
- `docker build -t pika2mqtt:operating-mode-repair-test .`
- `git diff --check`

## Acceptance and commit boundary

All four approved modes use exactly one controller POST, delayed convergence
can succeed, every failure leaves prior published state intact, status-model
writes are impossible in the implementation, documentation clearly requires a
corrected image, and all automated validation passes without contacting the
live inverter. Commit the remediation plan, code, tests, and documentation as
one independently revertible slice.

## Completion evidence

- Focused telemetry, configuration, and MQTT tests: 57 passed.
- Full repository suite: 78 passed, including the local web-gateway tests.
- `telemetry.py`, `mqtt_bridge.py`, and `pika2mqtt.py` compile successfully.
- The Docker image builds successfully as
  `pika2mqtt:operating-mode-repair-test`.
- The built image's missing-argument check exits with the expected status 2.
- `git diff --check` passes.
- No live inverter write or operating-mode change was performed.

Payload commit: `473e3295d2ca25081b32483d3b517a86ae297b63`
