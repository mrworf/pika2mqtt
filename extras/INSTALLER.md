# Preparing the inverter for Pika to MQTT

> Modifying the storage in a Pika Energy/Generac PWRcell inverter is at your own
> risk. Make a complete SD-card backup first. Do not change installer settings
> unless you understand their effect on the electrical system.

Pika to MQTT requires one persistent inverter modification: add your SSH public
key to root's `authorized_keys`. The runtime makes no other persistent or
ephemeral changes to the inverter. It uses SSH port forwarding to reach the
installer server on the inverter's localhost port 80.

These instructions assume Linux and the current UpdateFactory-based SD-card
layout, where partition 7 supplies the overlay's writable user data.

## 1. Create and protect a dedicated key

Use a key dedicated to this inverter integration:

```sh
ssh-keygen -t rsa -b 3072 -f pika-rsa
chmod 600 pika-rsa
```

Store `pika-rsa` outside the project repository. Only its `.pub` file belongs on
the inverter.

## 2. Back up and mount partition 7

Power down the inverter as directed by its service documentation, remove the
Raspberry Pi SD card, and create a complete image backup before modifying it.
After inserting it in the Linux machine, identify the device carefully. The
partition is commonly number 7, but the disk name is machine-specific.

Example only, using `/dev/sdb7`:

```sh
sudo mkdir -p /mnt/pika-user
sudo mount /dev/sdb7 /mnt/pika-user
```

Do not copy the example device name without verifying it locally.

## 3. Authorize the public key

```sh
sudo mkdir -p /mnt/pika-user/user/root/.ssh
cat pika-rsa.pub | sudo tee -a /mnt/pika-user/user/root/.ssh/authorized_keys >/dev/null
sudo chmod 700 /mnt/pika-user/user/root/.ssh
sudo chmod 600 /mnt/pika-user/user/root/.ssh/authorized_keys
sync
sudo umount /mnt/pika-user
```

Reinstall the card and boot the inverter. Verify access without changing the
inverter:

```sh
ssh -i pika-rsa -o BatchMode=yes root@192.168.1.42 hostname
```

## 4. Record the trusted host fingerprint

From the trusted LAN, obtain the inverter's ED25519 fingerprint:

```sh
ssh-keyscan -t ed25519 192.168.1.42 2>/dev/null \
  | ssh-keygen -lf - -E sha256
```

Record the complete `SHA256:...` value and independently confirm it through a
trusted connection or console. Configure it as `SSH_HOST_FINGERPRINT` when
starting Pika to MQTT.

## Persistence boundary

Only `authorized_keys` is assumed to survive reboots and firmware updates. Pika
to MQTT does not upload an HTTP proxy, add iptables rules, or create inverter
services. The external container owns tunnel monitoring, keepalives, reconnect
backoff, and optional LAN web access.

See the [main README](../README.md) for Docker configuration and web-gateway
instructions.
