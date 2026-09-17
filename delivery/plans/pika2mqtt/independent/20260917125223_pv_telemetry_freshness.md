# PV Telemetry Freshness and Request Resilience

Parent commit: `65f33eb0ec43d7915abee461c1e04ae0c1600789`

Represent disabled, disconnected, stale, and faulted PV Links accurately even
when the inverter's proprietary PV model endpoints return HTTP 500 or hang.
Keep `/devices` polling responsive and prevent detail-model failures from
recycling a healthy SSH tunnel.

Live investigation established that disabled PV Links remain present in
`/devices` with fresh `lastheard` values while their detail models can return
HTTP 500 or time out. Implementation and automated validation must use mocks;
no further live requests or mode changes are part of delivery.

## Delivery slices

1. [Fresh model state and Home Assistant availability](20260917125223_pv_telemetry_freshness_slice_01.md)
2. [Isolated detail scheduling and tunnel health](20260917125223_pv_telemetry_freshness_slice_02.md)

## Shared acceptance criteria

- Communication, enabled state, and fault state remain distinct signals.
- Detail values expire after 120 seconds without a successful refresh.
- Home Assistant shows unavailable for stale model-backed entities instead of
  presenting indefinite cached values or synthetic unknown states.
- Slow or failed detail models cannot block the 15-second `/devices` cadence.
- Detail HTTP 500s and read timeouts never recycle an otherwise healthy SSH
  tunnel.
- Existing MQTT state topics and entity unique IDs remain compatible.
