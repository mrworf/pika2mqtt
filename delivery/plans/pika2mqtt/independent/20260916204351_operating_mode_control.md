# Home Assistant Operating Mode Control

Parent commit: `3eec40050a1408291877c3d61a2fa6c1e0e3c058`

Add an explicitly enabled Home Assistant MQTT selector for Grid Tie, Self
Supply, Clean Backup, and Priority Backup while retaining the existing
read-only System Operating Mode sensor. Commands use the installer API form
POST contract and are reflected only after an authoritative readback.

No implementation or automated validation step may send a mode-changing
request to the live inverter. Live functional testing belongs exclusively to
the owner and is documented but not performed.

## Delivery slices

1. [Opt-in operating mode selector and command path](20260916204351_operating_mode_control_slice_01.md)

## Shared acceptance criteria

- Control is absent and no command topic is subscribed by default.
- The dedicated opt-in exposes only codes 1 through 4 and remains independent
  of the broad web gateway write setting.
- Retained, malformed, unknown, failed, and unconfirmed commands never change
  published state or claim success.
- Existing read-only sensor IDs and state JSON remain compatible.
- All automated write-path validation uses fakes or mocks only.
