# Pika to MQTT for Home Assistant

[![CI](https://github.com/mrworf/pika2mqtt/actions/workflows/docker-image.yml/badge.svg?branch=master)](https://github.com/mrworf/pika2mqtt/actions/workflows/docker-image.yml)
[![Container image](https://img.shields.io/badge/GHCR-pika2mqtt-2ea44f?logo=github)](https://github.com/mrworf/pika2mqtt/pkgs/container/pika2mqtt)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Bring your Pika Energy / Generac PWRcell solar system into Home Assistant over
local MQTT. Monitor solar production, grid flow, batteries and individual PV Links;
optionally change operating mode and enable or disable PV Links from dashboards
and automations. No Generac cloud service is involved.

pika2mqtt runs in Docker and creates devices through Home Assistant's
[MQTT integration](https://www.home-assistant.io/integrations/mqtt).
No custom Home Assistant integration or HACS installation is required. Monitoring
is enabled by default; write controls and web access are opt-in.

## What you get

| Device or feature | Available by default | Optional capabilities |
| --- | --- | --- |
| PWRcell inverter / system | Solar and inverter power, grid import/export power and energy, operating mode, string counts and aggregate communication/fault alerts | Operating-mode selector; additional firmware diagnostics |
| Each battery | State of charge, state of health, charging/discharging power, capacity and REbus status/measurements | Raw model and firmware flag diagnostics |
| Each battery module | State of charge and state of health | Cell count and minimum/maximum/average cell voltage and temperature diagnostics |
| Each learned PV Link | Power, status, enabled state, communication lost, fault summary, REbus measurements and supported SnapRS/PVRSS measurements | Enable/disable switch and detailed diagnostic flags |
| Firmware diagnostics | Last event, Active error count and definition-loading health | Individual register flags and extra enum/raw sensors, disabled by default |
| Installer website | Disabled | Authenticated local installer access and HTML/Markdown register reference |

Values depend on what your firmware and hardware expose. An unsupported or stale
measurement becomes unavailable; its absence does not mean the device has failed.
A battery module contains multiple physical cells: the installer API exposes
module SoC/SoH, not SoC/SoH for each physical cell.

The container maintains a resilient SSH tunnel to the inverter's local installer
server. It reconnects automatically after network outages and inverter reboots,
with activity visible in Docker logs. It does not install a proxy or change the
inverter firewall.

## Before you start

You need:

- An inverter prepared for root SSH access and its matching private key. Follow
  the [inverter preparation guide](extras/INSTALLER.md) first if this is not already
  configured. This is an unsupported integration and preparation requires modifying
  the inverter's storage; the guide covers backups and the persistent key.
- The inverter's address and independently verified ED25519 SSH host fingerprint.
- A Docker host that can reach the inverter on TCP/22 and your MQTT broker.
- A working MQTT broker, its address and any required credentials. Home Assistant
  must connect to the same broker with MQTT discovery enabled.
- A writable, persistent directory for inventory, register reference and discovery
  metadata, separate from the read-only SSH key.

If you use Home Assistant's Mosquitto broker app, see its
[setup and user instructions](https://github.com/home-assistant/addons/blob/master/mosquitto/DOCS.md).
An external broker also works. Use an address reachable from this container:
a Home Assistant internal app hostname may not resolve on a separate Docker host.

## First-time setup

### 1. Prepare the key and fingerprint

Keep the private key outside the repository and protect its permissions. Replace
the example paths and inverter address with your own:

```sh
chmod 600 /secure/path/pika-rsa
mkdir -p /secure/path/pika2mqtt-data

ssh-keyscan -t ed25519 192.168.1.42 2>/dev/null \
  | ssh-keygen -lf - -E sha256
```

Independently confirm the complete `SHA256:...` fingerprint through a trusted
connection or console. The container refuses a different host identity.

### 2. Start the container with Docker Compose

Save this as `compose.yaml`, replacing addresses, paths, fingerprint and MQTT
credentials. If your broker permits anonymous access, omit both credential fields.

```yaml
services:
  pika2mqtt:
    image: ghcr.io/mrworf/pika2mqtt:latest
    container_name: pika2mqtt
    restart: unless-stopped
    environment:
      HOSTNAME: "192.168.1.42"
      MQTT: "mqtt.local"
      MQTT_PORT: "1883"
      MQTT_USER: "pika2mqtt"
      MQTT_PASSWORD: "replace-with-your-mqtt-password"
      BASETOPIC: "house/energy"
      SSH_HOST_FINGERPRINT: "SHA256:replace-with-your-fingerprint"
    volumes:
      - /secure/path/pika-rsa:/key/id_rsa:ro
      - /secure/path/pika2mqtt-data:/data
```

```sh
docker compose up -d
docker compose logs -f pika2mqtt
```

No container ports need to be published for MQTT telemetry or Home Assistant
controls. The default primary polling interval is 15 seconds. To request five-second
primary polling, add `REFRESH: "5"` under `environment`; detailed models retain their
separate polling interval.

<details>
<summary>Alternative: docker run</summary>

Use the same prepared key, data directory and verified fingerprint:

```sh
docker run -d \
  --name pika2mqtt \
  --restart unless-stopped \
  -e HOSTNAME=192.168.1.42 \
  -e MQTT=mqtt.local \
  -e MQTT_USER=pika2mqtt \
  -e MQTT_PASSWORD='replace-with-your-mqtt-password' \
  -e BASETOPIC=house/energy \
  -e SSH_HOST_FINGERPRINT='SHA256:replace-with-your-fingerprint' \
  -v /secure/path/pika-rsa:/key/id_rsa:ro \
  -v /secure/path/pika2mqtt-data:/data \
  ghcr.io/mrworf/pika2mqtt:latest

docker logs -f pika2mqtt
```

Continue with the same verification steps below.

</details>

Images are published for `linux/amd64` and `linux/arm64`. Use `latest` for the
current build from `master`, or an immutable `sha-<commit>` tag to pin a deployment.
Version tags, when published, are also available.

### 3. Verify Home Assistant discovery

Make sure Home Assistant's MQTT integration is configured for the same broker.
Discovery is enabled by default in pika2mqtt with the `homeassistant` prefix.

Look for `SSH tunnel established`, `MQTT broker connected`,
`Starting the telemetry monitor` and `Loaded firmware register definitions` in
the logs. These messages may appear in a different order. Definition loading and
device measurements can take longer than the first primary poll.

In Home Assistant, open **Settings → Devices & services → MQTT** and inspect its
devices. Expect a PWRcell system/inverter, your batteries and their reported modules,
and one device per learned PV Link. Confirm that solar power, battery percentages
and operating mode match your system. Detailed entities appear as their data arrives.

Initially leave `PV_INVENTORY_FREEZE=false`. Confirm every expected PV Link is
discovered and its serial matches your array; check the `Learned PV Link` log messages.
Then optionally add `PV_INVENTORY_FREEZE: "true"` and recreate the container with
`docker compose up -d`. Frozen inventory keeps known strings monitored and reports
new, unrecognized strings without silently adding them. There is no fixed string
count; use the number installed in your system.

## Using the integration in Home Assistant

### Dashboards and the Energy dashboard

Use the discovered solar, grid, battery and per-string power sensors in dashboard
cards. Power is in watts, energy in kWh, and battery percentages are already
percentages—no scaling templates are needed.

Positive grid power means export; positive battery power means discharge.
Separate nonnegative grid import/export and battery charging/discharging power
sensors make dashboards easier to read.

For the Energy dashboard, use **Grid imported energy** and **Grid exported energy**,
which come from native inverter counters when available. Solar power is an
instantaneous measurement; the integration does not supply a dedicated solar-yield
energy counter. To derive solar energy, follow Home Assistant's
[energy guidance](https://www.home-assistant.io/docs/energy/) for integrating power
over time. Such energy is an estimate and depends on valid samples.

Do not use **Inverter accumulated energy** as solar production: inverter energy
can include other sources. Battery/module SoC and SoH are health and charge
measurements, not energy-throughput counters.

### Monitor each PV Link

Each string has separate signals with different meanings:

- **Enabled**: `True` when the PV Link is enabled, `False` when disabled.
- **Communication lost**: whether the string stopped communicating; a disabled
  string can still communicate normally. `True` means communication was lost;
  `False` means it was not.
- **Fault / Fault summary**: `True` when a fault is reported, `False` when no fault
  is reported; Fault summary provides its interpretation.
- **Detailed fault data unavailable**: optional diagnostic indicating reduced
  coverage when detailed models cannot be read (`True` means reduced coverage).

Use Home Assistant's entity picker to create an alert for an individual string or
the system's **String disconnected** and **String fault** aggregate sensors.
Read-only boolean entities are ordinary `sensor.*` entities displaying exactly
`True` or `False`: the value says whether the named statement is true. Automation
state comparisons must use the quoted strings `"True"` and `"False"`, not `on`/`off`.
Consider a sustained-state duration to avoid transient alerts, and handle
`unknown` and `unavailable` separately—neither means `False`. Existing users must
[migrate old binary-sensor references](docs/MIGRATION.md#truefalse-status-entity-migration).

Communication loss is declared after 120 seconds by default. Detailed values have
a two-minute freshness window; only affected entities become unavailable after
their source ages out. A normal core Fault reading does not prove every detailed
register was checked—use the coverage diagnostics when investigating.

PV production values below 0 W or above 5,000 W per string are rejected. The
affected power entity becomes unavailable, and aggregate solar power is withheld
if a primary string sample is invalid. Values are not clamped to a plausible number.

### Optional operating-mode and PV Link controls

Add this under your Compose service's `environment`, then recreate the container:

```yaml
OPERATING_MODE_CONTROL_ENABLED: "true"
```

This single flag enables both:

- A **System Operating Mode Control** selector with **Grid Tie**, **Self Supply**,
  **Clean Backup** and **Priority Backup**.
- An **Enabled Control** switch for each PV Link.

The read-only operating-mode and Enabled entities remain available. Other modes
can be read but are not offered as commands. Controls work in dashboards and
Home Assistant automations using the discovered selector and switches.

Commands are confirmed against inverter/controller readback before the new state
is published. A failed or unconfirmed command is logged and its write is not retried.
After enabling controls, manually verify one intended change when operationally
safe and restore your preferred state; check the inverter/installer interface and
container logs. Validate PV Links individually.

Use broker ACLs so only intended users can publish commands. The MQTT control flag
is independent of `WEB_ENABLED` and `WEB_WRITE_ENABLED`; no web gateway is required.
See the [control protocol and validation details](docs/TECHNICAL.md#optional-operating-mode-and-pv-link-control).

### Firmware events, flags and register reference

**Last event** reports the most recently observed device event, not an active fault
or a complete event history. **Active error count** includes classified current
indicators; it is unavailable when the required assessment is incomplete.
Several indicators can describe one physical problem.

Open a device's entity list to enable the diagnostic flags you want to monitor or
use in automations. Status/enum attributes list supported states; register attributes
include raw values, symbols and available descriptions. Definitions are loaded from
your inverter's firmware, so supported flags can differ between systems.
Individual flag sensors display `True` when the named firmware bit is active and
`False` when inactive. Positive flags such as Heartbeat Good are not faults simply
because they are `True`; inspect their description and policy classification.

The generated reference lists states, flags, numeric values, firmware descriptions
and application fault classifications:

- **File:** `/data/register_reference.md` inside the container, or
  `/secure/path/pika2mqtt-data/register_reference.md` with the example mount.
- **HTML:** `http://<docker-host>:8000/diagnostics/registers`.
- **Markdown download:** `http://<docker-host>:8000/diagnostics/registers.md`.

Web routes require the optional gateway and its authentication. Many firmware
symbols have no description; the reference explicitly says so. See the
[technical guide](docs/TECHNICAL.md#firmware-register-diagnostics-and-reference)
for policy overrides, unknown values and definition-loading behavior.

## Optional installer website

To add authenticated installer access and the register-reference pages to your
Compose service, configure:

```yaml
environment:
  WEB_ENABLED: "true"
  WEB_USERNAME: "operator"
  WEB_PASSWORD_FILE: "/run/secrets/pika-web-password"
volumes:
  - /secure/path/pika-web-password:/run/secrets/pika-web-password:ro
ports:
  - "8000:8000"
```

Merge these entries into the existing service; retain its MQTT settings, key and
data mounts. Create the password file with your chosen password and restrict its
permissions to `0600`, then recreate the container.

Open `http://<docker-host>:8000/` and authenticate. The gateway is read-only by
default; some installer actions need POST and therefore do not work in that mode.
Set `WEB_WRITE_ENABLED: "true"` only if you intend to allow installer configuration
changes. The reference routes always remain read-only.

Basic Auth does not encrypt traffic: expose the gateway on a trusted LAN or put
a TLS reverse proxy in front of it. `WEB_PASSWORD` is an alternative to the password
file; the file takes precedence. Web passwords have no CLI option, avoiding exposure
in process arguments.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `HOSTNAME` | required | Inverter IP address or DNS name |
| `MQTT` | required | MQTT broker hostname |
| `MQTT_USER` / `MQTT_PASSWORD` | empty | Optional MQTT credentials |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_CLIENT_ID` | derived from `HOSTNAME` | Stable broker client identifier |
| `BASETOPIC` | required | MQTT topic prefix |
| `IDRSA` | `/key/id_rsa` | Mounted inverter root private key |
| `SSH_HOST_FINGERPRINT` | required | Expected ED25519 SHA-256 fingerprint |
| `SSH_PORT` | `22` | Inverter SSH port |
| `SSH_LOCAL_PORT` | `18080` | Internal loopback tunnel port |
| `REFRESH` | `15` | `/devices` polling interval in seconds |
| `DETAIL_REFRESH` | `60` | Successful model polling interval in seconds |
| `DETAIL_REQUEST_TIMEOUT` | `20` | Background installer model request timeout in seconds |
| `DISCONNECT_AFTER` | `120` | Seconds before API/PV Link data is disconnected |
| `PV_INVENTORY_FILE` | `/data/pv_inventory.json` | Versioned learned PV Link inventory |
| `PV_INVENTORY_FREEZE` | `false` | Prevent newly observed PV Links from being learned |
| `REGISTER_POLICY_FILE` | bundled `register_policy.json` | Optional complete symbol-based fault-classification policy override |
| `HA_DISCOVERY_ENABLED` | `true` | Publish Home Assistant MQTT device discovery |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant discovery prefix |
| `OPERATING_MODE_CONTROL_ENABLED` | `false` | Allow approved operating-mode and PV Link enable controls through MQTT |
| `WEB_ENABLED` | `false` | Start the authenticated web gateway |
| `WEB_WRITE_ENABLED` | `false` | Forward methods other than GET/HEAD/OPTIONS |
| `WEB_LISTEN` | `0.0.0.0` | Gateway address inside the container |
| `WEB_PORT` | `8000` | Gateway port inside the container |
| `WEB_USERNAME` | required for web | Gateway Basic Auth username |
| `WEB_PASSWORD_FILE` | empty | Preferred gateway password secret file |
| `WEB_PASSWORD` | empty | Gateway password fallback |
| `IGNORE` | empty | Space-separated uppercase device serials to ignore |
| `DEBUG` | empty | Set to `--debug` for verbose logs |

The same behavior is available outside Docker through `pika2mqtt.py --help`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No Home Assistant devices | Same broker on both sides, valid MQTT credentials, Home Assistant MQTT discovery enabled, matching discovery prefix, broker ACLs and container logs |
| Mode selector or PV Link switches missing | Set `OPERATING_MODE_CONTROL_ENABLED=true` and recreate the container; the read-only sensors alone do not enable controls |
| An entity is disabled | Enable it from the device's entity list; extra diagnostics are disabled by default |
| Entities show unavailable | Inspect communication and model freshness separately; allow definitions to load, and check Detailed fault data unavailable and logs |
| Unknown status or flag | Compare its raw value and firmware reference; undocumented values are preserved rather than guessed |
| Command fails or mode does not change | Check confirmation errors in logs and verify manually in the installer interface; do not repeatedly issue writes |
| `SSH host-key mismatch` | Independently verify the inverter's current host key before updating the configured fingerprint |
| `SSH tunnel connection failed` | Check TCP/22 reachability, inverter address, authorized public key and private-key permissions |
| Repeated reconnects | Recovery backoff grows to at most 60 seconds; leave the container running and investigate network/inverter availability |
| Web returns 503 | Installer requests need an available tunnel; reference routes need loaded definitions |
| Web returns 502 | The installer server did not complete the forwarded request |
| Web returns 405 | Installer write methods are disabled, or a write was attempted on a read-only reference route |

Follow activity with `docker compose logs -f pika2mqtt` or `docker logs -f pika2mqtt`.
Set `DEBUG=--debug` for detailed diagnostics. Retained state is accompanied by
availability: an unavailable reading should not be treated as a cleared fault.

## Upgrades and advanced use

- [Migration and rollback guide](docs/MIGRATION.md) — older Docker images, topic
  changes, status-string compatibility and historical control fixes.
- [Technical reference](docs/TECHNICAL.md) — MQTT topics, control confirmation,
  firmware definitions, fault policy and polling/retry behavior.
- [Inverter preparation](extras/INSTALLER.md) — install the persistent root public key.
- [Container images](https://github.com/mrworf/pika2mqtt/pkgs/container/pika2mqtt)
  and [CI builds](https://github.com/mrworf/pika2mqtt/actions/workflows/docker-image.yml).

The normalized JSON MQTT topics can also be used without Home Assistant. Set
`HA_DISCOVERY_ENABLED=false` for another consumer, such as Telegraf or InfluxDB.
