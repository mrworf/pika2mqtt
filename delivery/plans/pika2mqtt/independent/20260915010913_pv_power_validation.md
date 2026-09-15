# PV Power Validation

Parent commit: `9f1f092cd1c955b8d855422aedd642fc2ee573b8`

Reject impossible PV Link production samples before publishing them. Values
from 0 W through 5000 W inclusive are valid; negative, non-finite, missing, and
greater-than-5000 W samples are unavailable. An invalid visible string also
makes aggregate solar power unavailable so a partial total is never presented
as complete.

## Delivery slices

1. [Reject and expose invalid PV power safely](20260915010913_pv_power_validation_slice_01.md)

## Shared acceptance criteria

- Invalid PV power is absent from normalized and raw published measurements.
- String connectivity, state, and fault telemetry remain available when only
  power is invalid.
- Home Assistant marks the affected string power and aggregate solar power
  sensors unavailable through dedicated retained availability topics.
- Each string logs one warning per invalid-data episode and an informational
  recovery message when valid data resumes.
- No cached or alternate measurement replaces an invalid primary sample.
