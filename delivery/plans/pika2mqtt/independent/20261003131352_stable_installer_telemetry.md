# Stable Installer Telemetry

Parent commit: `752702ed0665cbda332e7b864dab4f05027e441f`

Make installer telemetry resilient to the observed Generac firmware behavior:
valid inverter and battery model reads can take 8-13 seconds, while individual
PV model routes can consistently fail with a server-side Modbus exception even
though `/devices` and the controller directory remain healthy.

Keep five-second primary polling responsive, allow serialized detail requests
20 seconds to complete, use the controller directory as the authoritative
source for core per-string state, and expose reduced detailed-fault coverage
without making healthy strings unavailable. Persistent detail failures remain
eligible for recovery probes on an approximately 15-minute schedule.

## Delivery slices

1. [Stabilize slow detail reads and PV state](20261003131352_stable_installer_telemetry_slice_01.md)

## Shared acceptance criteria

- A valid detail response taking between 5 and 20 seconds is accepted while
  `/devices` polling remains independent.
- Persistent routes retry without synchronized 15-minute waves and expose
  actionable endpoint-health diagnostics.
- Fresh controller-directory data keeps each PV Link's core status and fault
  indication available even when PVLink/PVRSS detail routes return HTTP 500.
- Home Assistant separately reports unavailable detailed fault coverage.
- No automated validation changes a live operating mode or PV Link state.
