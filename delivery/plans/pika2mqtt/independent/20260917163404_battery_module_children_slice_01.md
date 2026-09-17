# Slice 01: Collect and publish battery module children

Parent commit: `55ee1df53ffa34c5f56867f23d8b3bafb80b56a3`
Payload commit: `949a1fa785f842b046061968ab16a05571f23a62`

## Goal and observable outcome

Home Assistant discovers one child device for each expected PWRcell battery
module. Each child normally shows module SoC and SoH, while optional diagnostic
entities expose cell count and min/max/average cell voltage and temperature.

## Scope and non-scope

- Fetch `lithium_ion_string` through its
  `lithium_ion_string_module` repeating-block route in the existing serial
  background detail worker.
- Normalize the one-based repeating records into a new top-level
  `battery_modules` snapshot collection, separate from existing aggregate
  battery state.
- Publish per-module state, model freshness, module presence, and Home
  Assistant child discovery beneath the existing battery device.
- Preserve previously discovered expected modules as unavailable when omitted.
- Do not change aggregate battery topics, discovery components, identifiers,
  availability, or normalized values; persist module inventory; derive alerts;
  expose writes; or model individual physical cells.

## Data and lifecycle behavior

- The detail key `lithium_ion_string` maps to
  `/device/<modID>/model/lithium_ion_string/lithium_ion_string_module` and uses
  the existing 120-second freshness, serial scheduling, and retry behavior.
- A valid integral `NMod` establishes expected indices 1 through N; valid
  positive repeating keys are unioned with them. Indices are capped at 32 to
  prevent corrupt responses from creating unlimited devices.
- Each normalized module has `index`, `present`, and available numeric fields
  mapped from `ModSoC`, `ModSoH`, `ModNCell`, cell voltage statistics, and cell
  temperature statistics. Missing values are omitted.
- Once the model is stale, module measurements are not normalized or
  published; model availability makes all previously discovered module
  entities unavailable. With a fresh model, an expected record that is absent
  receives module-presence unavailable.
- This feature is read-only; authorization and write permissions are not
  applicable.

## Public MQTT and Home Assistant interface

- State: `<base>/state/battery/<battery-serial>/module/<index>`.
- Model availability: `<base>/availability/battery/<battery-serial>/modules`.
- Presence: `<base>/availability/battery/<battery-serial>/module/<index>`.
- Module devices use stable IDs derived from parent battery serial plus index,
  and `via_device` points to the existing battery device.
- SoC and SoH are normal measurement entities. Cell count, voltage, and
  temperature entities are diagnostic and disabled by default.

## Validation, recovery, and commit boundary

- Fixture and unit tests cover the observed six-module response, exact field
  mapping, expected missing records, malformed and oversized indices, stale
  data, recovery, MQTT topics and availability, stable IDs, discovery defaults,
  and two-level device hierarchy.
- Regression tests prove aggregate battery state and discovery are unchanged
  when module telemetry succeeds or fails.
- Run focused telemetry/MQTT tests, syntax compilation, the complete unittest
  suite, CLI validation, and Docker build using mocks and fixtures only.
- Commit the plan, implementation, tests, fixture, and README together as one
  independently revertible slice.

## Implementation surfaces

- `telemetry.py`: model routing, bounded module normalization, and snapshot
  isolation from aggregate battery state.
- `mqtt_bridge.py`: retained module state and availability plus nested Home
  Assistant device discovery.
- `tests/fixtures/lithium_ion_string.json`: representative six-module API
  response.
- `tests/test_telemetry.py` and `tests/test_mqtt_bridge.py`: positive,
  malformed, missing, stale, recovery, hierarchy, and compatibility coverage.
- `README.md`: module terminology, topics, entities, and availability behavior.

## Acceptance criteria

- A valid six-module response produces six stable module children with enabled
  SoC and SoH entities.
- Expected missing records remain discovered but unavailable.
- Stale model data makes known module children unavailable and is not
  republished as current measurement state; recovery restores publication.
- Invalid or excessive module indices cannot create children beyond index 32.
- Aggregate battery state and discovery payloads are byte-for-byte unchanged
  whether module telemetry is present or absent.

## Completion evidence

- `python -m unittest tests.test_telemetry tests.test_mqtt_bridge -v`: 46 tests
  passed.
- `python -m unittest discover -s tests -v`: 75 tests passed. The sandboxed
  attempt could not open loopback sockets; the identical permitted rerun
  passed.
- `python -m py_compile telemetry.py mqtt_bridge.py pika2mqtt.py`: passed.
- CI command-line missing-argument exit validation in the built image: passed
  with the expected exit code 2.
- `docker build -t pika2mqtt:battery-modules-test .`: passed.
- `git diff --check`: passed.
