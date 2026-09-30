# Operating Mode Authoritative Write Repair

Parent commit: `a35889fc26852a540ca1d30cb714e03dd2f01719`

Repair the opt-in Home Assistant operating-mode control so it writes the
authoritative `SysMd` point in Generac SunSpec model 64200 (`REbus_dir`) on the
system controller instead of posting to read-only status model 64208
(`inverter_status`). Confirm the requested value against both models before
publishing it as current.

The existing selector, command topic, option allowlist, unique IDs, default-off
flag, and read-only operating-mode sensor remain unchanged. No live write or
automated mode change is allowed during implementation or validation.

## Delivery slices

1. [Write and confirm the authoritative operating mode](20260930153836_operating_mode_write_repair_slice_01.md)

## Shared acceptance criteria

- A command sends one `0_SysMd` fixed-block form POST only to
  `/device/1/model/REbus_dir`.
- Confirmation reads occur immediately and then every five seconds for no more
  than 30 seconds, ending only when `REbus_dir` and `inverter_status` agree
  with the request.
- Failed or unconfirmed commands never publish a requested value as current.
- Existing MQTT and Home Assistant public interfaces remain compatible.
- All implementation validation uses mocks and fixtures; the owner performs
  the eventual live mode-change test.
