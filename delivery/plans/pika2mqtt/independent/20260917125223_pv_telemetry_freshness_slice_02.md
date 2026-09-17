# Slice 02: Isolated detail scheduling and tunnel health

Depends on Slice 01.

## Goal and observable outcome

Primary `/devices` polling and MQTT state publication continue at their normal
cadence even if PV detail endpoints are slow, return HTTP 500, or time out.
Only primary connectivity failures contribute to SSH tunnel recycling.

## Scope and non-scope

- Run detail-model collection in one serial background worker with its own
  persistent HTTP session.
- Space requests and add retry staggering while retaining per-endpoint
  exponential backoff capped at 15 minutes.
- Synchronize device/cache/health state used by polling, detail collection,
  snapshots, and operating-mode confirmation.
- Classify detail HTTP errors and read timeouts as endpoint failures, not SSH
  transport failures. Keep `/devices` connection failures authoritative for
  tunnel health.
- Do not parallelize detail requests, change public MQTT contracts, alter the
  inverter filesystem, or perform live validation.

## Behavior and lifecycle

- The collector starts one detail worker after MQTT and tunnel orchestration is
  initialized and stops/joins it during shutdown.
- The main collector requests `/devices`, snapshots, and publishes without
  waiting for detail work.
- The detail worker serially selects due models from the latest device
  inventory, performs at most one request at a time, and waits briefly between
  requests so retries are not synchronized bursts.
- Device disappearance or modID changes are resolved from the latest inventory
  before each request. Stale results for a changed device mapping are discarded.
- A recovered endpoint resets backoff and restores freshness/availability on
  the next primary publication.
- This is internal read-only collection; authorization is not applicable.

## Validation and recovery

- Test that a blocked detail request does not block repeated primary polls.
- Test serial request execution, retry backoff/cap/staggering, worker shutdown,
  modID-change result rejection, and recovery.
- Test detail HTTP 500/read timeout leave tunnel failure counters untouched,
  while primary connection failure still reports to the supervisor.
- Test operating-mode commands remain serialized safely with telemetry state.
- Run focused tests, the complete unittest suite, CLI validation, syntax
  compilation, and Docker build. All network behavior must be mocked.

## Implementation surfaces and commit boundary

Update telemetry request orchestration, application thread lifecycle, focused
tests, and operational documentation. Commit the completed second slice only
after the repository-wide validation passes.
