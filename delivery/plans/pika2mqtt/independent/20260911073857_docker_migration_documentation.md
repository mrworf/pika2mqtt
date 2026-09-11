# Docker Migration Documentation

Parent commit: `8a90379955cd1e2d535dc4a845c11e3b6037eabf`

## Goal

Add an explicit migration path for existing Docker users moving from the
on-inverter port-8000 proxy to the resilient SSH tunnel and optional container
web gateway.

## Decisions

- Cover both `docker run` and Docker Compose deployments.
- Identify the mandatory private key and host fingerprint, changed network
  path, unchanged MQTT/Home Assistant contract, optional authenticated web
  access, validation steps, and rollback preparation.
- Require no inverter cleanup; the authorized root key remains required.
- Make no executable behavior changes.

## Delivery slice

1. [Docker migration guide](20260911073857_docker_migration_documentation_slice_01.md) — delivered in the `Document Docker migration` commit

## Acceptance

An existing operator can safely recreate either deployment style, recognize
all breaking configuration changes, validate tunnel recovery in container logs,
and understand how old direct web access maps to the new opt-in gateway.
