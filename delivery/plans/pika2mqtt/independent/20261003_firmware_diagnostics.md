# Firmware-driven diagnostics and register reference

Approved plan: load installed SunSpec XML at startup, SSH reconnect, and firmware
changes; decode currently collected models using explicit firmware values; preserve
raw data and unknown indicators. An editable symbol-based policy classifies faults.
No additional HTTP polling endpoints or control changes are included.

Expose last event, active error count and disabled-by-default individual flags on
existing MQTT devices. Preserve existing topic/entity identity and partial coverage
semantics. Reconcile obsolete generated entities using a persisted manifest.

Generate /data/register_reference.md and authenticated HTML/Markdown views at
/diagnostics/registers and /diagnostics/registers.md when the web gateway is enabled.
Document source descriptions, unsupported meanings and policy classifications.

Log initial observations and changes, suppress duplicates, and distinguish
observations after gaps from timestamped device events. Definition failures retry
with backoff/jitter capped at 60 seconds without blocking primary polling.

Validate parsing, dynamic firmware values, sparse/unknown bits, invalid definitions,
policies, availability, discovery cleanup, logs, file writes and authenticated web
routes. Run full unittest suite, CLI validation and Docker smoke checks. Live checks
are read-only.

## Slice index

1. [Runtime diagnostics and reference](20261003_firmware_diagnostics_slice_01.md):
   one coherent end-to-end feature; loading, interpretation, publication and user
   reference share a single definition snapshot and must be validated together.

Transaction parent: a49c999798588d6d933fa2009a71e0dcdf507cf8. Worktree initially clean.
