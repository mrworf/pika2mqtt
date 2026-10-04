# Slice 01: explicit truth states and safe discovery migration

## Goal and boundary

Home Assistant read-only flags display True/False instead of On/Off or OK/Problem.
Includes discovery, cached firmware definitions, migration documentation, and tests.
Excludes switch/select controls, polling, telemetry interpretation, and live writes.
Depends only on the approved governing plan; one independently testable slice.

## End-to-end behavior

Normalize boolean components at the MQTT discovery boundary, giving new component
keys and unique IDs a `_truth` suffix. Use guarded templates that return explicit
strings only for actual booleans or valid firmware symbol lists; missing/null values
return an unknown state, never False. Preserve the source availability topics so
stale observations remain unavailable. Retain original polarity for inverted flags.

Before replacement publication, send component removal entries for legacy keys in
an otherwise complete device config. Retain new keys on reconnect/restart; never
remove the replacement keys. Normalize cached binary definitions even when firmware
loading is unavailable, preserving checksums and firmware-derived metadata. Persist
normalized discovery only after successful MQTT publication. Failed cleanup must
prevent replacement publication and be retryable.
Retain legacy tombstones in final discovery and store their IDs separately in the
manifest, so offline HA also sees removals after reconnection or later restarts.

No new permissions or inverter commands. Existing MQTT discovery authorization and
controls are unchanged. Raw JSON remains unchanged.

## Surfaces and validation

Expected surfaces: mqtt_bridge.py, register_definitions.py, relevant tests, README,
technical/migration guides. Test truth polarity, dynamic firmware flags, definition
health, missing/null source values, availability, migration tombstones, cached
manifests without definitions, firmware changes, repeated publication/reconnect,
failed cleanup, and control preservation. Render templates in tests, not just
compare their text. Run `python -m unittest tests.test_mqtt_bridge
tests.test_register_definitions -v`, then `python -m unittest discover -s tests -v`
and CI command-line validation. Build/smoke the container if environment permits.

## Acceptance and commit

No binary entities remain in final retained discovery, no legacy binary resurrection
from persisted manifests, no false clearances from absent data, no control changes.
Docs show sensor-domain migration and string comparisons. One commit includes all
slice-owned changes after validation. Commit identified by this unique slice-plan
path in Git history (avoids a self-referential SHA). HA UI verification remains a
manual post-deployment step.

## Result

Implemented discovery-boundary normalization for all read-only booleans, guarded
True/False templates, distinct replacement identities, retained legacy tombstones,
and persisted legacy IDs across firmware changes/restarts. Cached pre-upgrade flags
are migrated without loaded definitions. Controls and raw JSON remain unchanged.
Updated user/migration/technical docs and both generated reference formats. Added
test-only Jinja2 dependency and CI installation to render templates under strict
undefined handling without adding a runtime dependency.

Validation: 48 targeted tests and all 125 full-suite tests passed. The first sandboxed
full-suite run was blocked by loopback socket permissions; the same suite passed
with loopback access. CLI missing arguments returned the required exit code 2.
`git diff --check` passed. Docker build `pika2mqtt:truth-status-check` succeeded;
network-disabled container smoke checks passed for discovery conversion and
generated reference. No live inverter requests, control writes, deployment, or push.
HA frontend rendering is intentionally left for the user's post-deployment check.
Commit provenance: `git log -- delivery/plans/pika2mqtt/independent/20261003_true_false_statuses_slice_01.md`.
