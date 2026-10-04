# Upgrading an existing installation

[Home Assistant setup and capabilities](../README.md) · [Technical reference](TECHNICAL.md)

Use this guide when upgrading an existing deployment. For a new installation,
start with the [README setup instructions](../README.md#first-time-setup).

## Updating an existing SSH-based installation

If your deployment already uses the SSH tunnel and Home Assistant discovery,
keep its configuration, private-key mount and persistent data directory. Review
the [compatibility changes](#compatibility-changes) below, save your configuration
and back up `/data`, then update your Compose service:

```sh
docker compose pull pika2mqtt
docker compose up -d pika2mqtt
docker compose logs -f pika2mqtt
```

For `docker run`, recreate the container with its existing settings and mounts.
Keep a record of the previous image tag or ID for rollback. The container rename
and old-proxy instructions below apply to legacy installations.

## Legacy proxy and publisher migration

The current integration is not a drop-in replacement for either the older
port-8000 proxy image or the previous MQTT publisher. Home Assistant discovery is
enabled by default and replaces the legacy per-value topic tree with retained JSON
state. Existing dashboards and automations that directly reference topics such
as `<base>/solar_total/output`, `<base>/battery_<serial>/charge`, or
`<base>/connected/state` must be moved to the discovered entities or the new
topics in the [technical reference](TECHNICAL.md#mqtt-and-home-assistant).
The old topics stop updating immediately after the upgrade. In particular,
battery percentage is now a real percentage rather
than a value multiplied by ten, and the incorrect interval `*_kwh` topics have
been removed.

The `HOSTNAME`, `MQTT`, `MQTT_USER`, `MQTT_PASSWORD`, `BASETOPIC`, `IGNORE`,
and `/key/id_rsa` interfaces remain available.

## Compatibility changes

### True/False status entity migration

Read-only boolean entities now display literal `True`/`False` instead of
On/Off or OK/Problem. This is a **breaking Home Assistant entity change**: they
move from `binary_sensor.*` to ordinary `sensor.*` entities. Discovery component
keys and unique IDs gain `_truth`; Home Assistant chooses the resulting entity IDs
from names and existing registry entries, so use its entity picker rather than
guessing the ID from the unique ID.

Affected entities include Communication lost, Fault, Enabled, Detailed fault data
unavailable, String disconnected, String fault, Register definitions available,
and individual firmware-defined bit flags. Their names and polarity are unchanged:
Communication lost True means lost communication; Enabled False means disabled.
Unknown/unavailable does not mean False. Default-enabled/diagnostic settings and
firmware descriptions/classifications are preserved.

After updating:

1. Wait for MQTT discovery and definitions to load. Legacy binary discovery is
   removed automatically; persisted firmware flags are also migrated if definition
   loading is temporarily unavailable.
   If you installed the initial True/False image but still see OK/Problem, pull
   the corrected image and recreate the container. Its discovery removal entries
   now include the required platform; it republishes corrected discovery on startup.
   No manual MQTT cleanup is needed for this correction.
2. Replace old binary-sensor references in dashboards, automations, scripts, and
   helpers with the newly discovered sensors. Re-enable optional diagnostic flags
   and reapply any custom names/settings to their new entities as needed.
3. Replace `on` comparisons with the quoted string `"True"` and `off` with `"False"`.
   Keep unavailable handling separate. For example, a state trigger becomes:

   ```yaml
   triggers:
     - trigger: state
       entity_id: sensor.replace_with_actual_communication_lost_entity
       to: "True"
       for: "00:02:00"
   ```

Switches/selectors and their control commands are unchanged. Raw MQTT JSON still
uses boolean true/false, not strings. On rollback to an older image, the previous
binary entities return. Restore the backed-up pre-upgrade
`/data/register_discovery.json` (or move the newer manifest aside), and remove the
newer `_truth` discovery components or this integration's retained device discovery
configs before restarting the old image to avoid duplicate entities. Preserve the
PV inventory and do not clear unrelated MQTT discovery topics.

### Earlier telemetry and control changes

If an existing deployment already sets `OPERATING_MODE_CONTROL_ENABLED=true`,
upgrading also exposes an `Enabled Control` switch for every PV Link. Review
MQTT ACLs and Home Assistant user permissions before upgrading, or set the flag
to `false` until PV Link control is desired. Existing JSON state topics remain
compatible; the read-only Enabled entity follows the True/False migration above.

The current integration also changes PV Link availability semantics. Core status and
fault monitoring now use the controller directory and remain available when
firmware rejects optional per-PV model reads. The existing Fault and Status
entity identifiers were unchanged by that telemetry update (Fault now follows
the True/False migration above). A new disabled-by-default `Detailed fault
data unavailable` diagnostic reports when PVLink/PVRSS-specific coverage is
missing. Existing deployments need no configuration change; optionally set
`DETAIL_REQUEST_TIMEOUT` if the 20-second default is unsuitable for a
particularly slow appliance.

Firmware register decoding is now loaded over SSH from the inverter at startup.
Existing MQTT topics and control commands remain intact (boolean entities now
follow the True/False migration above), but
Status text follows the firmware symbol names in lowercase. For example, older
`input_over_voltage` states become `over_voltage_input`; `status_code` now preserves
the complete code instead of masking its low four bits. Review automations that
compare status strings or numeric codes. Status/fault interpretation is unavailable
until definitions have loaded; measurements and control polling continue.

New Last event and Active error count diagnostics are enabled automatically.
Individual register flags are disabled by default. `/data` also stores the generated
register reference and discovery manifest; keep this volume writable and persistent.

## Deployment requirements

- The SSH private key is now mandatory and should be mounted read-only.
- `SSH_HOST_FINGERPRINT` is mandatory.
- The Docker host must reach the inverter on TCP/22.
- The container no longer connects to or maintains port 8000 on the inverter.
- Mount a writable directory at `/data`; this stores learned PV Link identities,
  the register reference and discovery manifest. Keep it separate from the
  read-only SSH key mount.
- `-t` is no longer required when starting the container.
- Published images now use `ghcr.io/mrworf/pika2mqtt`; replace the previous
  `mrworf/pika2mqtt` Docker Hub image name in Docker or Compose configurations.

Before updating, retain the old container or its Compose configuration so it
can be restored. Stop the old container before starting the new version; an old
instance left running may continue reinstalling the obsolete Pi-side proxy.
Compose users should also give the currently running image an immutable local
rollback tag before pulling `latest`:

```sh
docker image tag "$(docker inspect --format '{{.Image}}' pika2mqtt)" \
  pika2mqtt:pre-ssh-tunnel
```

Obtain and independently verify the fingerprint as shown in the
[setup guide](../README.md#1-prepare-the-key-and-fingerprint), make sure the existing
private key has mode `0600`, and then migrate using the appropriate
deployment style.

### Existing `docker run` deployment

Pull the new image, preserve the stopped old container for rollback, and create
the replacement with the additional fingerprint setting:

```sh
docker pull ghcr.io/mrworf/pika2mqtt:latest
docker stop pika2mqtt
docker rename pika2mqtt pika2mqtt-pre-ssh-tunnel

docker run -d \
  --name pika2mqtt \
  --restart unless-stopped \
  -e HOSTNAME=192.168.1.42 \
  -e MQTT=mqtt.local \
  -e BASETOPIC=house/energy \
  -e SSH_HOST_FINGERPRINT='SHA256:replace-with-your-fingerprint' \
  -v /secure/path/pika-rsa:/key/id_rsa:ro \
  -v /secure/path/pika2mqtt-data:/data \
  ghcr.io/mrworf/pika2mqtt:latest
```

Carry over any existing MQTT credentials, `IGNORE`, `DEBUG`, custom `IDRSA`, or
other settings from the old container. Do not copy the example addresses or
credentials literally.

### Existing Docker Compose deployment

Update the service to include the fingerprint and a read-only key mount. A
minimal complete service is:

```yaml
services:
  pika2mqtt:
    image: ghcr.io/mrworf/pika2mqtt:latest
    container_name: pika2mqtt
    restart: unless-stopped
    environment:
      HOSTNAME: 192.168.1.42
      MQTT: mqtt.local
      BASETOPIC: house/energy
      SSH_HOST_FINGERPRINT: "SHA256:replace-with-your-fingerprint"
    volumes:
      - /secure/path/pika-rsa:/key/id_rsa:ro
      - /secure/path/pika2mqtt-data:/data
```

Keep any existing MQTT credentials and other optional environment values, then
recreate the service:

```sh
docker compose pull pika2mqtt
docker compose up -d --force-recreate pika2mqtt
```

### Verify and roll back

Follow startup and recovery state in the container logs:

```sh
docker logs -f pika2mqtt
```

A successful migration logs `Connecting SSH tunnel`, followed by
`SSH tunnel established`, `MQTT broker connected`, and `Starting the telemetry
monitor`. Confirm that Home Assistant creates the PWRcell device and its PV Link
children. Fingerprint mismatches and corrupt inventory files are fatal;
network, SSH, MQTT, and inverter reboot failures remain logged and recover with
bounded backoff.

Initially leave `PV_INVENTORY_FREEZE=false`. After all expected PV Links have
appeared in Home Assistant and the inventory log (six on the system used during
development), restart with `PV_INVENTORY_FREEZE=true`. Learning is additive and
has no built-in limit, so future arrays with a different number of strings work
without code changes. With learning frozen, a new unrecognized string is
reported in the system state but is not added as a monitored child.

To intentionally relearn the array, stop the container, delete only the file
configured by `PV_INVENTORY_FILE`, start with learning unfrozen, verify the
expected strings, then freeze again. Never delete, move, or change permissions
on `/key/id_rsa` as part of this process.

No inverter cleanup is required. The old proxy process and firewall rule are
ignored by the new container and normally disappear on an inverter reboot. A
stale proxy file is harmless. Keep the authorized root key because the SSH
tunnel requires it.

If another tool or bookmark previously used `http://<inverter>:8000`, that
endpoint is no longer maintained. Enable the web gateway described in the
[README](../README.md#optional-installer-website),
publish its port explicitly, and use `http://<docker-host>:8000` instead. The
gateway requires Basic Auth and remains read-only unless
`WEB_WRITE_ENABLED=true` is set.

To roll back a `docker run` deployment, stop and remove only the replacement,
restore the preserved container name, and restart it:

```sh
docker stop pika2mqtt
docker rm pika2mqtt
docker rename pika2mqtt-pre-ssh-tunnel pika2mqtt
docker start pika2mqtt
```

For Compose, restore the saved Compose file, set the service image to
`pika2mqtt:pre-ssh-tunnel`, and recreate the service. Rollback does not require
changing the inverter or removing its authorized key.


## Historical operating-mode control fixes

Older images either sent the request to read-only `inverter_status` or omitted
the installer API's required `0_` fixed-block field prefix. A selection could
therefore appear successful briefly or be silently ignored. Do not use
operating-mode automation with an affected image; pull and recreate the
container with the corrected image first.
