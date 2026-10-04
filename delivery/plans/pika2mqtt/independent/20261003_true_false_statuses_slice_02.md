# Slice 02: valid Home Assistant discovery removals

Parent commit: 96348c882bd3806bb17bc40a264ca727fbed845c. Worktree initially clean.
Goal: Home Assistant accepts boolean migration and removes old OK/Problem entities.
Approved correction: removal entries must retain platform (`binary_sensor` for
legacy booleans, the original platform for obsolete firmware components), not {}.
Source: https://www.home-assistant.io/integrations/mqtt/#device-discovery-payload.

Depends on slice 01. Change discovery publication, regression tests, migration
guidance, and durable test-contract instructions only. Preserve truth polarity,
availability, firmware definitions, controls, raw MQTT state, and polling.
No inverter requests or changes to deployed installations are authorized/needed.

Publication: replace all empty removal entries with platform-only mappings, both
intermediate and retained final legacy removals. Exclude removal entries from
persisted diagnostic definitions; retain historical legacy IDs separately.
Obsolete diagnostic removals retain their original platform. Failed publication
continues to retry and must not mark replacements as published.

Tests: enforce the documented component platform requirement on every device
discovery message in the fake MQTT client. Positive platform-only removals pass;
empty/untyped components fail. Exercise retained offline/restart migration,
firmware pruning, cached manifests without loaded definitions, and failure retry.
Acceptance: no removal with {} is published, no removal stored as a definition,
replacement truth entities remain sensors and control behavior remains unchanged.

Run targeted MQTT/register tests, full unittest suite with loopback access, CLI
validation, Docker build/smoke, and git diff checks. Commit the coherent correction
once all validation passes and push master as explicitly requested.
Provenance: git log -- delivery/plans/pika2mqtt/independent/20261003_true_false_statuses_slice_02.md.

## Result

Corrected legacy removals in intermediate and retained discovery to platform-only
binary_sensor entries; obsolete firmware entities retain their own platform.
Removal entries are excluded from persisted diagnostic definitions. The test MQTT
client now enforces platform/unique-ID presence on every device message, with
positive typed-removal and negative empty/untyped regression cases. Added a durable
AGENTS.md test-contract lesson and image-update recovery instructions.

Demonstrated the regression test failing on the old production code, then passing
with the fix. All 49 targeted and 126 full-suite tests passed (loopback enabled for
local fake servers). CLI validation returned 2 as required; diff checks passed.
Docker image pika2mqtt:discovery-removal-check built successfully, and its isolated
network-disabled smoke check verified typed removals, replacement sensors, and
absence of removal entries in the manifest. No live inverter/HA requests or writes.
