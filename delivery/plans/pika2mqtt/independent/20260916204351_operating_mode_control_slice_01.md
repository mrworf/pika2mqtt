# Slice 01: Opt-in operating mode selector and command path

Parent commit: `3eec40050a1408291877c3d61a2fa6c1e0e3c058`

## Goal and observable outcome

When `OPERATING_MODE_CONTROL_ENABLED=true`, Home Assistant displays a separate
System Operating Mode Control selector that can request Grid Tie, Self Supply,
Clean Backup, or Priority Backup. The selector remains non-optimistic and shows
only the mode confirmed by inverter telemetry. Default deployments remain
read-only and unchanged.

## Scope and non-scope

- Add a dedicated default-off configuration flag, MQTT command subscription,
  selector discovery, queued execution, installer form POST, and readback.
- Preserve the existing System Operating Mode sensor and all web-gateway flags.
- Reject retained commands and values outside the four-label allowlist.
- Document broker ACL guidance and a user-owned manual validation procedure.
- Do not expose Safety Shutdown, Remote Arbitrage, or Sell as commands; retry a
  command automatically; alter the SSH key or inverter filesystem; or execute a
  live mode-changing request during implementation or validation.

## Dependencies and end-to-end behavior

Home Assistant publishes an exact option label to
`<base>/command/system_operating_mode`. The MQTT callback validates it and
hands its numeric code to the collector without blocking the MQTT network
thread. Pending selections coalesce to the newest value. The collector resolves
the current inverter `modID`, posts `SysMd=<code>` as form data to
`/device/<modID>/model/inverter_status`, immediately reads that model back, and
publishes a fresh snapshot only when the returned code matches.

The selector uses `<base>/state/inverter`, extracts
`system_operating_mode`, combines service and inverter availability in `all`
mode, and is non-optimistic with non-retained commands. MQTT broker credentials
and ACLs are the command authorization boundary; pika2mqtt adds no separate
identity system.

## State transitions, validation, and recovery

- Disabled: do not subscribe to the command topic or publish the selector.
- Valid non-retained command: replace any queued selection and wake the
  collector; log acceptance without claiming completion.
- Successful POST plus matching readback: update cached model telemetry,
  publish the confirmed state, and log success.
- Invalid/retained command: reject and log without queueing.
- Missing inverter, HTTP/transport failure, malformed response, or mismatched
  readback: log failure, preserve existing state, and do not retry.
- Disconnect/reconnect: command subscription is restored only when enabled;
  no command is retained or replayed by pika2mqtt.

## Implementation surfaces and tests

- Update runtime configuration/orchestration, telemetry writes, MQTT discovery
  and command handling, Docker defaults, README, and adjacent unit tests.
- Positive mocked tests cover codes 1-4, dynamic modID, exact form body,
  matching readback, coalescing, enabled discovery, and subscription.
- Negative mocked tests cover default-off behavior, retained/unknown/malformed
  payloads, disabled commands, missing inverter, request/HTTP errors, malformed
  or mismatched readback, and preservation of the existing sensor.
- Run focused configuration, telemetry, MQTT, and orchestration tests; the full
  unit suite; syntax compilation; and the Docker build. None may contact the
  live inverter.

## Acceptance and commit boundary

The default remains read-only, enabled users receive a confirmed four-option
selector, all unsafe or failed inputs are inert, the manual-only live test
boundary is documented, and all automated checks pass with mocks. Commit the
plan, runtime, tests, and documentation as one independently revertible slice.
