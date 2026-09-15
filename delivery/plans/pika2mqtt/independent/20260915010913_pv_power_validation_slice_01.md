# Slice 01: Reject and expose invalid PV power safely

Parent commit: `9f1f092cd1c955b8d855422aedd642fc2ee573b8`

## Goal and observable outcome

Prevent impossible negative or above-5-kW per-string production values from
appearing in MQTT or Home Assistant. During bad samples, only the relevant
power sensors become unavailable; monitoring of connection, status, faults,
and other measurements continues.

## Scope and non-scope

- Accept finite numeric PV Link power from 0 W through 5000 W inclusive.
- Omit invalid normalized power and invalid detailed REbus raw power.
- Omit aggregate solar power whenever any visible, non-ignored PV Link lacks a
  valid primary `/devices` power sample.
- Publish retained per-string and aggregate power-quality availability and use
  it in Home Assistant discovery.
- Log an invalid episode once per string and log recovery after a valid sample.
- Document the bounds and unavailable behavior.
- Do not validate inverter, grid, or battery power; make the limit configurable;
  substitute cached data; or alter connection/fault semantics.

## Dependencies and end-to-end behavior

This extends the existing `/devices` collector, retained MQTT state topics, and
Home Assistant device discovery. A visible PV Link is assessed every successful
poll. Its primary sample controls normalized `power_w`, per-string power
availability, aggregate inclusion, and aggregate availability. Invalid cached
detail-model `REbus_status.P` is also removed from the PV Link payload, but does
not override a valid authoritative `/devices` sample.

The bridge publishes `available` or `unavailable` on
`<base>/availability/power/pv/<serial>` and
`<base>/availability/power/solar`. Existing service, inverter, and PV Link
availability remains unchanged and combines with the new topic using Home
Assistant's `all` availability mode. Authorization is not applicable because
collection and publication remain read-only.

## State transitions, validation, and recovery

- Valid primary sample: publish `power_w`, make that string's power available,
  and include it in the solar total if every visible string is valid.
- Invalid or missing primary sample: omit `power_w` and directional derivatives,
  publish string power unavailable, and omit the aggregate total.
- Invalid detailed PV `REbus_status.P`: omit both `rebus_power_w` and the raw
  `P` scalar from the published PV payload.
- First invalid sample for a string: warn with serial, offending source/value,
  and accepted range. Continued invalid polls do not repeat the warning.
- Valid recovery while the string is visible: log recovery and make power
  available. A disappearing string clears its invalid episode without claiming
  recovery; normal connection aging remains authoritative.

## Implementation surfaces and tests

- Update `telemetry.py`, `mqtt_bridge.py`, `tests/test_telemetry.py`,
  `tests/test_mqtt_bridge.py`, and `README.md`.
- Positive tests cover 0 W, normal output, exactly 5000 W, aggregate production,
  availability, and recovery.
- Negative tests cover negative, above-limit, NaN, infinity, and missing primary
  samples; raw detailed rejection; suppressed aggregate totals; episode-based
  logging; and preservation of non-power telemetry.
- Run `python3 -m unittest tests.test_telemetry tests.test_mqtt_bridge -v`,
  `python3 -m unittest discover -s tests -v`, syntax compilation for runtime
  modules, and `docker build . --file Dockerfile --tag pika2mqtt:test`.

## Acceptance and commit boundary

All invalid values are absent, Home Assistant receives explicit power-only
unavailability, valid boundary values remain observable, logging is bounded,
and existing health telemetry is unaffected. Commit this plan, production code,
tests, and documentation as one independently revertible slice.
