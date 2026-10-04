# Explicit True/False Home Assistant statuses

Product: pika2mqtt. Parent commit: f65a6ea93ef4503278f20a6e0f5bed8edaffe0b7.
Initial worktree: clean. Approved breaking entity migration; no compatibility binaries.

Replace all read-only boolean binary entities with ordinary MQTT sensors displaying
exactly `True` or `False`. Preserve names, polarity, descriptions, severity,
availability, diagnostic/default-enabled settings, raw JSON booleans, and firmware
definitions. Missing/null/stale data must not become False. Controls, nonboolean
sensors, polling, and inverter interpretation remain unchanged.

New discovery component keys and unique IDs use `_truth`. Remove legacy components
explicitly before publishing replacements, preserve unrelated entities, and normalize
cached firmware discovery during startup without definitions. Update README,
migration/technical guides, and generated firmware reference with truth meanings and
automation migration guidance.

## Slice index

1. [Truth sensors and migration](20261003_true_false_statuses_slice_01.md): the complete
   discovery/availability behavior, tests, and documentation in one coherent commit.

Validation: targeted MQTT/register tests, full unittest suite, CI CLI validation,
Docker build/smoke when available. No live control writes. Actual HA rendering is a
post-deployment manual check, not claimed by mocked tests.
