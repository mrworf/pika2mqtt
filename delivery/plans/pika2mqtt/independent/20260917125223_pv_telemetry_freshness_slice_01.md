# Slice 01: Fresh model state and Home Assistant availability

Parent commit: `65f33eb0ec43d7915abee461c1e04ae0c1600789`

## Goal and observable outcome

A communicating but disabled PV Link remains connected and reports Enabled
Off. Model-backed values remain usable through a 120-second transient-failure
grace period, then their Home Assistant entities become unavailable and stale
cached data no longer participates in normalized state or fault assessment.

## Scope and non-scope

- Track freshness and health separately for each device model.
- Build normalized state from fresh models only while retaining stale raw
  payloads as annotated diagnostics.
- Publish retained per-model availability and apply it to the corresponding
  Home Assistant entities.
- Rename the displayed Disconnected entity to Communication lost without
  changing its unique ID.
- Do not change `/devices` connectivity semantics, MQTT state topics, entity
  unique IDs, request scheduling, SSH behavior, or operating-mode behavior.

## Behavior and state transitions

- A successful model response records `last_success` and makes the model fresh.
- A failed refresh records the current error but cached data remains fresh
  until its age exceeds 120 seconds.
- Once stale, the cached payload remains under raw diagnostics with explicit
  health metadata, but normalized fields omit it.
- Fault assessment is complete only when fresh `REbus_status`,
  `pvlink_status`, and `pvrss_telemetry` are all present.
- Model availability topics are retained and combine with service and PV Link
  communication availability. `/devices`-backed entities do not depend on
  detail-model availability.
- This is read-only telemetry; authorization is not applicable.

## Validation and recovery

- Test fresh success, a failed refresh within grace, expiry at the 120-second
  boundary, independent model-family availability, incomplete stale fault
  assessment, and recovery after a later success.
- Test disabled-but-responsive state remains connected and Enabled Off.
- Test discovery names, stable unique IDs, model availability dependencies,
  and retained availability publications.
- Run telemetry and MQTT bridge tests, syntax compilation, then inspect the
  slice diff before commit.

## Implementation surfaces and commit boundary

Update telemetry normalization/health, MQTT publication/discovery, adjacent
tests, and README semantics. Commit these changes and this plan as one coherent
slice before beginning request scheduling work.
