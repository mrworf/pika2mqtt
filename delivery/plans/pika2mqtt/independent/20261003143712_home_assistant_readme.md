# Refocus documentation on Home Assistant users

Approved plan: rewrite README around capabilities, prerequisites and first-time
setup. Compose is the primary installation path, docker run the alternative.
Explain MQTT integration/discovery, device verification, PV learning/freezing,
dashboards and Energy dashboard, optional controls, diagnostic entities and the
generated register reference. Retain the complete environment table, actual
15-second polling default, optional REFRESH=5 and practical troubleshooting.

Move migration/rollback and compatibility warnings to docs/MIGRATION.md. Move
MQTT topics, endpoint/write mechanics, retry behavior and diagnostic-policy details
to docs/TECHNICAL.md. Link both guides and the inverter preparation guide. Preserve
security and operational constraints, power direction/units, native grid counters,
battery module versus physical-cell distinction and control/web flag independence.

Validate documented capabilities/defaults against code, Compose/command syntax,
relative links and badge/image targets. Review a complete first-time-user journey.
This documentation-only change requires no executable tests or live requests.

Product id: pika2mqtt. Parent commit: 8c8e6dfe2412c71db01da1138e523d18100f1ebd.
Initially clean worktree. Owned paths: README.md, docs/MIGRATION.md,
docs/TECHNICAL.md and this governing/slice plan pair.

## Slice index

1. [Home Assistant onboarding and reference guides](20261003143712_home_assistant_readme_slice_01.md)
