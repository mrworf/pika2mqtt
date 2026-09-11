# Pika to MQTT

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
  mrworf/pika2mqtt:latest
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
  mrworf/pika2mqtt:latest
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
