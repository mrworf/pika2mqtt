# Pika to MQTT

[![CI](https://github.com/mrworf/pika2mqtt/actions/workflows/docker-image.yml/badge.svg?branch=master)](https://github.com/mrworf/pika2mqtt/actions/workflows/docker-image.yml)
[![Container image](https://img.shields.io/badge/GHCR-pika2mqtt-2ea44f?logo=github)](https://github.com/mrworf/pika2mqtt/pkgs/container/pika2mqtt)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Pika to MQTT reads a Pika Energy/Generac PWRcell inverter's local installer API
and publishes its power data to MQTT. Everything stays on the local network; no
Generac cloud service is involved.

Generac only permits the installer site to be reached from localhost or its
provisioning network. Pika to MQTT therefore opens a resilient SSH tunnel to the
inverter's localhost port 80. It does not install a proxy, alter the inverter's
firewall, or persist anything on the inverter beyond the authorized root SSH
key described in [the installer guide](extras/INSTALLER.md).

## Docker quick start

The SSH private key is required. Keep it outside the repository, set its mode to
`0600`, and mount it read-only:

```sh
chmod 600 /secure/path/pika-rsa

docker run -d \
  --name pika2mqtt \
  --restart unless-stopped \
  -e HOSTNAME=192.168.1.42 \
  -e MQTT=mqtt.local \
  -e BASETOPIC=house/energy \
  -e SSH_HOST_FINGERPRINT='SHA256:replace-with-your-fingerprint' \
  -v /secure/path/pika-rsa:/key/id_rsa:ro \
  ghcr.io/mrworf/pika2mqtt:latest
```

Obtain the inverter's ED25519 host-key fingerprint from a trusted LAN before
starting the container:

```sh
ssh-keyscan -t ed25519 192.168.1.42 2>/dev/null \
  | ssh-keygen -lf - -E sha256
```

Confirm the fingerprint through a trusted connection or console. The container
will refuse a different host identity rather than disabling SSH verification.

MQTT authentication remains optional:

```sh
-e MQTT_USER=pika2mqtt -e MQTT_PASSWORD='mqtt-password'
```

Follow tunnel state and reconnect attempts through normal container logs:

```sh
docker logs -f pika2mqtt
```

The supervisor detects SSH exits and unusable tunnels, uses SSH keepalives,
retries with jittered exponential backoff capped at 60 seconds, and resumes
polling automatically after an inverter reboot or network outage.

The CI workflow tests every branch and pull request. Successful pushes to
`master` publish `linux/amd64` and `linux/arm64` images to GitHub Container
Registry as `latest`, `master`, and an immutable `sha-<commit>` tag. A tag such
as `v1.2.3` publishes the corresponding container tag as well.

## Migrating an existing Docker installation

Releases using the resilient SSH tunnel are not drop-in replacements for the
older port-8000 proxy image. MQTT topics and the `HOSTNAME`, `MQTT`,
`MQTT_USER`, `MQTT_PASSWORD`, `BASETOPIC`, `IGNORE`, and `/key/id_rsa`
interfaces remain compatible, so Home Assistant does not need to be
reconfigured. The transport requirements have changed:

- The SSH private key is now mandatory and should be mounted read-only.
- `SSH_HOST_FINGERPRINT` is mandatory.
- The Docker host must reach the inverter on TCP/22.
- The container no longer connects to or maintains port 8000 on the inverter.
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

Obtain and independently verify the fingerprint as shown above, make sure the
existing private key has mode `0600`, and then migrate using the appropriate
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
`SSH tunnel established` and `Starting the monitor`. Confirm that Home
Assistant begins receiving its existing MQTT topics. Fingerprint mismatches are
fatal; network, SSH, and inverter reboot failures remain logged and retry with
bounded backoff.

No inverter cleanup is required. The old proxy process and firewall rule are
ignored by the new container and normally disappear on an inverter reboot. A
stale proxy file is harmless. Keep the authorized root key because the SSH
tunnel requires it.

If another tool or bookmark previously used `http://<inverter>:8000`, that
endpoint is no longer maintained. Enable the web gateway described below,
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

## Optional installer website

The web gateway is disabled by default. To expose authenticated, read-only
access on the Docker host's port 8000, create a password secret and explicitly
publish the port:

```sh
printf '%s\n' 'choose-a-strong-password' >/secure/path/pika-web-password
chmod 600 /secure/path/pika-web-password

docker run -d \
  --name pika2mqtt \
  --restart unless-stopped \
  -e HOSTNAME=192.168.1.42 \
  -e MQTT=mqtt.local \
  -e BASETOPIC=house/energy \
  -e SSH_HOST_FINGERPRINT='SHA256:replace-with-your-fingerprint' \
  -e WEB_ENABLED=true \
  -e WEB_USERNAME=operator \
  -e WEB_PASSWORD_FILE=/run/secrets/pika-web-password \
  -v /secure/path/pika-rsa:/key/id_rsa:ro \
  -v /secure/path/pika-web-password:/run/secrets/pika-web-password:ro \
  -p 8000:8000 \
  ghcr.io/mrworf/pika2mqtt:latest
```

Open `http://<docker-host>:8000/` and enter the configured Basic Auth
credentials. Read-only mode permits GET, HEAD, and OPTIONS, which is sufficient
to load the installer interface and inspect values. Other methods return 405.

To allow installer POST and other write methods, add:

```sh
-e WEB_WRITE_ENABLED=true
```

Write mode can change safety- and operation-relevant inverter configuration.
Enable it only when needed. Basic Auth does not encrypt traffic, so publish this
port only on a trusted LAN or place a TLS reverse proxy in front of it.

`WEB_PASSWORD` may be used instead of `WEB_PASSWORD_FILE`, but a mounted secret
file is preferred. The file takes precedence if both are set.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `HOSTNAME` | required | Inverter IP address or DNS name |
| `MQTT` | required | MQTT broker hostname |
| `MQTT_USER` / `MQTT_PASSWORD` | empty | Optional MQTT credentials |
| `BASETOPIC` | required | MQTT topic prefix |
| `IDRSA` | `/key/id_rsa` | Mounted inverter root private key |
| `SSH_HOST_FINGERPRINT` | required | Expected ED25519 SHA-256 fingerprint |
| `SSH_PORT` | `22` | Inverter SSH port |
| `SSH_LOCAL_PORT` | `18080` | Internal loopback tunnel port |
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
Passwords intentionally have no web-gateway command-line option so they do not
appear in process arguments.

## MQTT topics

Devices publish below `<base>/<type>_<serial>/`. Power-producing values use
`output`; consuming values use `input`. Raw signed power and interval energy
values are also published by the current collector.

Examples:

```text
house/energy/solar_00010003BEEF/output
house/energy/battery_00010003BEEF/input
house/energy/battery_00010003BEEF/output
house/energy/battery_00010003BEEF/charge
house/energy/solar_total/output
house/energy/grid/input
house/energy/grid/output
house/energy/connected/state
```

Battery charge is multiplied by ten to avoid a floating-point MQTT payload, so
`945` represents 94.5%. Grid readings depend on correctly installed current
transformers.

The MQTT data can be consumed directly by Home Assistant or stored through
tools such as Telegraf and InfluxDB for visualization in Grafana.

## Troubleshooting

- `SSH host-key mismatch`: stop and verify whether the inverter host key
  legitimately changed after service or firmware replacement. Update the
  configured fingerprint only after independent verification.
- `SSH tunnel connection failed`: confirm port 22 reachability, the root public
  key installation, private-key permissions, and the configured address.
- Repeated reconnect logs: the delay will increase to at most 60 seconds; the
  container should remain running and recover automatically.
- Web gateway returns 503: the SSH tunnel is currently disconnected.
- Web gateway returns 502: the tunnel exists but the installer server did not
  complete that request.
- Web gateway returns 405: write methods are disabled; enable
  `WEB_WRITE_ENABLED` only if the operation is intended.

The historical port-8000 Python proxy and firewall workaround are no longer
used or supported.
