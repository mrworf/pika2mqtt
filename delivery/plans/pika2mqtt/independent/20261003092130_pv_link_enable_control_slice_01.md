# Slice 01: Add authoritative PV Link enable controls

Parent commit: `8d2875aaef8177b66cffd384833fb4ccf2a541b9`

## Goal and observable outcome

With `OPERATING_MODE_CONTROL_ENABLED=true`, each currently mapped PV Link has
an independent Home Assistant Enabled Control switch. ON and OFF requests are
queued without blocking MQTT, written to the verified controller repeating
block, and reflected in Home Assistant only after authoritative confirmation.

## Scope and non-scope

- Collect `/device/1/model/REbus_dir/devices` as controller telemetry.
- Resolve a PV Link by its 12-hex RCP serial split into manufacturer, device
  type, and device ID, plus its current `/devices` Modbus unit ID.
- Require exactly one current LCM controller at module ID 1 and exactly one
  positive numeric directory block match for each command.
- Prefer fresh directory `Ena` data for the existing Enabled state and fall
  back to `pvlink_status` when the directory is unavailable.
- Add a non-optimistic Home Assistant switch with command topic
  `<base>/command/pv/<serial>/enabled`, exact ON/OFF payloads, QoS 1 commands,
  and service/inverter/PV/directory availability.
- Coalesce repeated pending commands for the same PV Link while retaining
  commands for distinct links in insertion order.
- Reuse the existing default-off control flag and document the expanded trust
  boundary and broker ACL.
- Do not alter the SSH key, write files on the inverter, retry a control POST,
  or execute a live PV Link state change.

## Dependencies and end-to-end behavior

The detail collector reads the controller directory through the existing
serialized installer-API path. MQTT validates the exact wildcard topic,
12-hex serial, non-retained message, and exact ON/OFF payload before handing a
boolean request to the collector. The collector queues per-link requests and
processes them sequentially.

Immediately before a write, telemetry performs a fresh directory read and
re-resolves the requested serial against current device inventory. If `Ena`
already equals the request it updates the authoritative cache and returns
without posting. Otherwise it sends exactly one form POST to
`/device/1/model/REbus_dir/devices` with `<block>_Ena` and then confirms with
an immediate fresh directory read followed by five-second reads through the
30-second deadline. Identity is re-resolved on every read so a changed mapping
cannot confirm a stale write target.

## Failure and recovery behavior

- Missing, malformed, stale, ignored, or ambiguous device/directory mappings
  fail before the POST.
- Unknown topics, invalid serials or payloads, retained messages, and commands
  received while controls are disabled are rejected.
- A POST error fails immediately and is not retried.
- Confirmation transport errors, malformed payloads, mapping changes, and
  mismatched values remain eligible until timeout.
- Timeout logs the requested state and last observed state, does not overwrite
  the prior cache, and publishes no requested state.
- Normal polling remains authoritative after success or failure.

## Implementation surfaces and tests

- Update `telemetry.py` for controller-directory collection, strict mapping,
  write/no-op behavior, confirmation, state precedence, and control
  availability.
- Update `mqtt_bridge.py` for wildcard command handling, control availability,
  and Home Assistant switch discovery.
- Update `pika2mqtt.py` for per-link command coalescing and sequential work.
- Update focused tests with a stateful fake installer handler that returns HTTP
  success for wrong fields without changing state, proving that `Ena`,
  `0_Ena`, stale indexes, and the wrong endpoint cannot falsely succeed.
- Update `README.md` with configuration, topics, ACLs, behavior, migration
  impact, and manual validation guidance.

Required validation:

- `python -m unittest tests.test_telemetry tests.test_configuration tests.test_mqtt_bridge -v`
- `python -m py_compile telemetry.py mqtt_bridge.py pika2mqtt.py`
- `python -m unittest discover -s tests -v`
- CI missing-argument validation with installed dependencies
- `docker build -t pika2mqtt:pv-link-control-test .`
- `git diff --check`

## Acceptance and commit boundary

Both enable and disable paths use the exact verified route, block index, and
numeric value; no-op and all mapping failures avoid writes; delayed
confirmation can succeed; timeout cannot synthesize state; discovery and
availability are correct; the existing Enabled entity remains compatible; and
all automated validation passes without a live control write. Commit the plan,
implementation, tests, and documentation as one independently revertible
slice.

## Completion evidence

- Focused telemetry, configuration, and MQTT tests: 69 passed.
- Full repository suite: 90 passed, including local web-gateway tests.
- `telemetry.py`, `mqtt_bridge.py`, and `pika2mqtt.py` compile successfully.
- The Docker image builds successfully as
  `pika2mqtt:pv-link-control-test`.
- The built image's missing-argument check exits with the expected status 2.
- `git diff --check` passes.
- Tests prove that HTTP success for `Ena`, `0_Ena`, a stale block index, or the
  fixed-block endpoint does not change directory state.
- No live inverter write or PV Link state change was performed.

Payload commit: `abaf4c7eb45c7d95f32831b7d614a26883d56728`
