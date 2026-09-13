# Slice 05: System operating mode telemetry

Parent commit: `23005b8266e4ac80c889101481cd7bebbb7419ee`

## Goal and observable outcome

Expose the inverter's `SysMd` value as normalized System Operating Mode data in
MQTT and as an enabled Home Assistant sensor. A live system reporting `3`
appears as `Clean Backup` rather than requiring users to inspect raw diagnostics.

## Scope and non-scope

- Decode model 64208 values 0 through 6 using the inverter's installed SunSpec
  model labels, stable symbol names, and descriptions.
- Add human label, symbol, numeric code, and description to inverter state JSON.
- Add an enabled read-only Home Assistant sensor named `System Operating Mode`.
- Document the measurement and cover known and unknown values in tests.
- Do not change operating mode, expose write controls, alter the web gateway,
  or touch the inverter filesystem or SSH key.

## Dependencies and behavior

This extends the existing `inverter_status` polling and parent device discovery;
no new endpoint is required. Unknown future/malformed codes remain observable
with an `UNKNOWN_<code>` key and `Unknown (<code>)` label. Missing `SysMd`
produces null normalized fields and an unknown Home Assistant state. The sensor
inherits service and inverter availability. Authorization is not applicable
because this path remains read-only.

## Implementation and validation

- Update telemetry normalization, parent-device discovery, fixture assertions,
  discovery assertions, and MQTT documentation.
- Positive test: `SysMd=3` yields `CLEAN_BACKUP`, `Clean Backup`, code 3, and the
  matching model description.
- Negative tests: unknown and missing values are preserved safely without an
  exception or incorrect known label.
- Run focused telemetry/MQTT tests, the complete unit suite, syntax checks, and
  the Docker build.

## Acceptance and commit boundary

The normalized MQTT payload and Home Assistant discovery both expose System
Operating Mode without changing any write behavior. Commit this plan, runtime,
tests, and documentation as one independently revertible slice.
