# PV Link Enable Control

Parent commit: `8d2875aaef8177b66cffd384833fb4ccf2a541b9`

Extend the existing opt-in control surface with independent Home Assistant
switches for enabling and disabling each current PV Link. Resolve every write
from the controller's fresh `REbus_dir/devices` repeating block and use the
verified one-based `<block>_Ena` form field rather than assuming a device ID or
block index.

The existing read-only Enabled entity remains available. The existing
`OPERATING_MODE_CONTROL_ENABLED` switch authorizes both operating-mode and PV
Link controls, remains disabled by default, and does not authorize automated
tests to change the live system.

## Delivery slices

1. [Add authoritative PV Link enable controls](20261003092130_pv_link_enable_control_slice_01.md)

## Shared acceptance criteria

- Every write is mapped from a fresh controller directory using all identity
  fields and the PV Link's current Modbus unit ID.
- A command posts once to `/device/1/model/REbus_dir/devices` with exactly
  `<one-based-block>_Ena=0|1`; an already-satisfied command performs no POST.
- Success is published only after fresh directory reads confirm the requested
  state, while failure preserves the prior published state.
- Home Assistant receives a non-optimistic switch per PV Link without changing
  the existing Enabled binary sensor.
- Automated tests model the installer's silent HTTP-success behavior for
  malformed fields and never write to the live inverter.
