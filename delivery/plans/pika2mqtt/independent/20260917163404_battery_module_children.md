# PWRcell Battery Module Child Devices

Parent commit: `55ee1df53ffa34c5f56867f23d8b3bafb80b56a3`

Collect Generac SunSpec model 804's repeating battery-module block and expose
one Home Assistant child device per module. The observed cabinet reports six
modules containing 13 physical cells each; the API provides SoC and SoH per
module, not per individual electrochemical cell.

Existing aggregate battery MQTT topics, entity unique IDs, normalized values,
and Home Assistant discovery remain unchanged. No live requests, writes, or
operating-mode changes are part of implementation or validation.

## Delivery slices

1. [Collect and publish battery module children](20260917163404_battery_module_children_slice_01.md)

## Shared acceptance criteria

- Module SoC and SoH are enabled by default as separate Home Assistant child
  devices beneath the existing aggregate battery device.
- Cell count plus voltage and temperature statistics are disabled-by-default
  diagnostics.
- Expected-but-missing modules remain visible and unavailable.
- Stale or failed module telemetry never changes aggregate battery availability
  or leaves module measurements presented as current.
- Existing aggregate battery contracts remain compatible.
