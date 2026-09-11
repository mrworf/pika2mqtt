# Home Assistant Telemetry and MQTT Reliability

Parent commit: `496509de067b8b3b6e904398cc915e33f204da5e`

Replace the legacy MQTT leaf topics with a reliable, retained, Home Assistant
device-discovery contract for the inverter, grid, battery, and every PV Link.
The collector uses the model endpoints defined by the inverter UI, preserves
operation when optional model routes fail, learns PV Link identities into a
separate persistent inventory, and never modifies the inverter or SSH key.

## Delivery slices

1. [Telemetry and PV inventory](20260911160836_home_assistant_telemetry_slice_01.md)
2. [MQTT reliability and Home Assistant discovery](20260911160836_home_assistant_telemetry_slice_02.md)
3. [Container configuration and migration documentation](20260911160836_home_assistant_telemetry_slice_03.md)

## Shared acceptance criteria

- `/devices` remains the authoritative 15-second presence/power source; detail
  models are fetched every 60 seconds with bounded failure backoff.
- Strings become disconnected only after 120 seconds absent/stale, not merely
  because a model route returns an error.
- MQTT uses QoS 1 retained state and `connected`/`disconnected` availability,
  recovers after broker loss, and republishes after Home Assistant birth.
- Home Assistant receives a parent system device plus battery and PV Link child
  devices with normalized measurements and separate connection/fault signals.
- Learning is unlimited and additive; `PV_INVENTORY_FREEZE=true` prevents new
  identities from entering the versioned inventory.
- The old MQTT contract is removed and its breaking migration is documented.
- No implementation reads, writes, copies, or changes the permissions of the
  SSH private key beyond its existing use by the SSH client.
