# Slice 02: MQTT reliability and Home Assistant discovery

## Goal and outcome

Publish the telemetry snapshot through a reconnect-safe MQTT contract that
Home Assistant discovers automatically as a system parent with battery and PV
Link child devices.

## Scope

- Use stable MQTT client identity, QoS 1, retained states/discovery, explicit
  connection callbacks, bounded reconnect backoff, and publish-result logging.
- Configure a retained LWT and graceful shutdown payload using exactly
  `connected` and `disconnected`.
- Gate telemetry until CONNACK, retain the latest snapshot through outages, and
  republish state/discovery after reconnect or `homeassistant/status=online`.
- Publish JSON state below `<base>/state/...` and availability below
  `<base>/availability/...`; remove the old per-value publishing loop.
- Publish Home Assistant device discovery by default with stable unique IDs,
  correct device/state classes, units, parent/child `via_device`, separate
  per-string and aggregate connection/fault entities, and diagnostics disabled
  by default where appropriate.

## Non-scope

MQTT TLS, legacy topic compatibility, inverter writes, SSH transport changes,
and Docker/README migration text.

## Dependencies and behavior

Depends on slice 01 snapshots. Service availability reflects MQTT session
state; inverter availability becomes disconnected only after the API has been
unavailable for 120 seconds; each string adds its own availability topic. Child
entities require all applicable availability topics. Grid export and battery
discharge are positive signed power, with separate nonnegative directional
entities. Native Wh counters are converted to kWh and named for their actual
meaning. Authentication is valid only with both username and password or with
neither. MQTT has no authorization beyond configured broker credentials.

## Interfaces

- State: `<base>/state/system`, `/inverter`, `/grid`,
  `/battery/<serial>`, `/pv/<serial>`.
- Availability: `<base>/availability/service`, `/inverter`,
  `/pv/<serial>`.
- Discovery: `<discovery-prefix>/device/<stable-device-id>/config`, default
  prefix `homeassistant`.

## Validation

- Positive: CONNACK gating, retained QoS 1 publication, discovery schemas and
  identifiers, LWT setup, graceful disconnect, reconnect replay, HA birth
  replay, correct units/directions, and parent/child relationships.
- Negative: partial credentials, failed publish acknowledgements, broker loss,
  inverter loss, unavailable strings, malformed birth messages, and unknown
  status values.
- Run focused MQTT/discovery tests, related collector tests, then
  `python3 -m unittest discover -s tests -v` with loopback permission if needed.

## Acceptance and commit boundary

A fake broker/client test demonstrates the complete new contract and recovery
behavior. Commit runtime MQTT changes and their tests as one slice.
