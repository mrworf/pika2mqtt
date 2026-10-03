# Slice 01: Home Assistant onboarding and reference guides

Goal: a new Home Assistant user understands capabilities and reaches discovered
devices without reading migration history. Depends on the existing implementation;
changes only Markdown documentation and this plan.

Entry/journey: README overview -> prerequisites and inverter preparation -> MQTT
setup -> authenticated Compose/docker run deployment -> logs and discovered devices
-> dashboards, alerts, optional controls and diagnostic reference. No runtime state
or authorization changes. Preserve key mounts, fingerprint verification, MQTT ACLs,
web authentication and independent write flags in instructions.

Implementation surfaces: rewrite README; relocate all existing migration warnings
and rollback instructions into docs/MIGRATION.md; retain detailed MQTT/policy/control
and polling semantics in docs/TECHNICAL.md. Fix relative and section links after moves.
Keep default REFRESH=15 and offer REFRESH=5 explicitly as an optional choice.

Validation: inspect source-backed capability/default statements, parse embedded YAML
with available YAML tooling, check shell snippets without executing them, validate
relative links/anchors and review documentation diff. Check the happy path plus
missing discovery/control flags, unavailable readings, absent descriptions and
upgrade rollback guidance. No tests, Docker build or inverter requests for this
documentation-only slice. Preserve accurate explanations when reorganizing text.

Acceptance: monitoring-first setup is complete for both deployment styles; capability
table distinguishes defaults and optional entities; Energy dashboard instructions
do not claim accumulated inverter energy is solar production; technical and upgrade
guides preserve useful details; links and examples resolve correctly.

Commit all slice-owned documentation and plans together after validation.
Commit provenance: git log -- delivery/plans/pika2mqtt/independent/20261003143712_home_assistant_readme_slice_01.md.

## Completion and validation

Delivered a 361-line Home Assistant-first README (previously 645 lines), with
capability table, Compose and docker run onboarding, discovery verification,
inventory learning/freezing, dashboards/energy, alerts, optional controls and
register-reference access. Preserved migration and rollback in docs/MIGRATION.md
and detailed MQTT/control/policy/polling content in docs/TECHNICAL.md.

Validated 18 relative links/anchors, five YAML blocks with PyYAML and nine shell
snippets with bash -n (no snippet execution). Confirmed Compose image, MQTT
credentials, read-only key/persistent data mounts, no unnecessary published ports,
badge workflow target, startup log text and documented defaults against source.
Official Home Assistant MQTT, Mosquitto and energy documentation links were checked.
Documentation diff and whitespace checks passed. No executable tests or live
inverter requests were needed; runtime files and credentials remain unchanged.
