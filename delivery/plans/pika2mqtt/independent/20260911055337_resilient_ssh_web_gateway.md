# Resilient SSH Web Gateway

Parent commit: `2146b91d373527c45707efe86f542d2e3ff58240`

## Goal

Replace the unreliable on-device HTTP proxy and firewall mutation with a
self-healing SSH local forward managed by the pika2mqtt container. Continue to
publish the existing Home Assistant MQTT contract through that transport and
optionally expose the complete Generac installer website through an
authenticated, read-only-by-default HTTP gateway.

The root SSH key remains the inverter's only persistent modification.

## Decisions and constraints

- An SSH private key and configured SHA-256 inverter host-key fingerprint are
  required. There is no direct-port or on-device repair fallback.
- SSH reconnects indefinitely for transient failures with jittered exponential
  backoff, keepalives, health tracking, and Docker-visible lifecycle logging.
- The web gateway is disabled by default and always requires Basic
  Authentication when enabled.
- Read-only web mode permits GET, HEAD, and OPTIONS. A separate opt-in permits
  all installer HTTP methods, including POST.
- Docker port publication remains an explicit operator choice.
- MQTT topics, values, parsing, and energy calculations do not change.
- No file, process, firewall rule, or startup setting is installed on the
  inverter.

## Delivery slices

1. [Slice 01: resilient SSH transport](20260911055337_resilient_ssh_web_gateway_slice_01.md) — complete in `3461da7`
2. [Slice 02: authenticated installer web gateway](20260911055337_resilient_ssh_web_gateway_slice_02.md) — complete in `03eae47`
3. [Slice 03: container packaging and operator documentation](20260911055337_resilient_ssh_web_gateway_slice_03.md) — complete in the `Package resilient SSH gateway` commit

## Acceptance

- Rebooting or temporarily disconnecting the inverter produces clear container
  logs, bounded reconnect attempts, automatic recovery, and resumed polling.
- The complete installer site loads through the optional gateway; writes are
  denied by default and work only after the write opt-in is enabled.
- The runtime never uploads a proxy or changes inverter firewall state.
- The live `pika-rsa` private key is ignored and never committed.
