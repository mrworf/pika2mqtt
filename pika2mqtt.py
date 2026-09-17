#!/usr/bin/env python3
"""Publish the local Generac PWRcell installer API to MQTT."""

import argparse
import logging
import os
import signal
import sys
import threading
import time

from mqtt_bridge import MqttBridge, stable_id
from pika_transport import SshTunnelConfig, SshTunnelSupervisor, TransportConfigurationError
from telemetry import InstallerTelemetry, InventoryError, OperatingModeError, PvInventory
from web_gateway import GatewayConfigurationError, InstallerWebGateway, WebGatewayConfig, resolve_web_password


def environment_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"{name} must be true or false")


class CollectorThread(threading.Thread):
    def __init__(self, telemetry, publisher, transport, refresh=15):
        super().__init__(name="installer-monitor", daemon=True)
        self.telemetry = telemetry
        self.publisher = publisher
        self.transport = transport
        self.refresh = refresh
        self.stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._command_lock = threading.Lock()
        self._pending_operating_mode = None

    def stop(self):
        self.stop_event.set()
        self._wake_event.set()

    def request_operating_mode(self, label, code):
        with self._command_lock:
            previous = self._pending_operating_mode
            self._pending_operating_mode = (label, code)
        if previous is not None:
            logging.info(
                "Replacing pending operating mode command %s with %s",
                previous[0],
                label,
            )
        self._wake_event.set()

    def _take_pending_operating_mode(self):
        with self._command_lock:
            command = self._pending_operating_mode
            self._pending_operating_mode = None
        return command

    def _process_pending_operating_mode(self):
        command = self._take_pending_operating_mode()
        if command is None:
            return False
        label, code = command
        if not self.transport.wait_available(timeout=0):
            logging.error(
                "Operating mode command %s failed: SSH tunnel is unavailable", label
            )
            return False
        try:
            snapshot = self.telemetry.set_system_operating_mode(code)
        except OperatingModeError as error:
            logging.error("Operating mode command %s failed: %s", label, error)
            return False
        self.publisher.publish_snapshot(snapshot)
        logging.info("Operating mode command confirmed: %s", label)
        return True

    def run(self):
        logging.info("Starting the telemetry monitor")
        next_poll = 0.0
        while not self.stop_event.is_set():
            if not self.publisher.wait_connected(timeout=1):
                continue
            self._process_pending_operating_mode()
            if time.monotonic() >= next_poll:
                if self.transport.wait_available(timeout=1):
                    snapshot = self.telemetry.poll()
                else:
                    snapshot = self.telemetry.current_snapshot()
                self.publisher.publish_snapshot(snapshot)
                next_poll = time.monotonic() + self.refresh
            wait_for = min(1.0, max(0.0, next_poll - time.monotonic()))
            self._wake_event.wait(wait_for)
            self._wake_event.clear()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Pika-2-MQTT - local Generac PWRcell telemetry",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("hostname", help="IP or FQDN of the Pika system")
    parser.add_argument("mqtt", help="MQTT broker hostname")
    parser.add_argument("basetopic", help="MQTT state and availability topic prefix")
    parser.add_argument("--user", help="MQTT broker user")
    parser.add_argument("--password", help="MQTT broker password")
    parser.add_argument("--mqtt-port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    parser.add_argument("--mqtt-client-id", default=os.getenv("MQTT_CLIENT_ID"))
    parser.add_argument("--idrsa", default="/key/id_rsa", help="Root SSH key for the Pika system")
    parser.add_argument("--ssh-host-fingerprint", default=os.getenv("SSH_HOST_FINGERPRINT"), help="Expected SHA-256 SSH host-key fingerprint")
    parser.add_argument("--ssh-port", type=int, default=int(os.getenv("SSH_PORT", "22")), help="Inverter SSH port")
    parser.add_argument("--ssh-local-port", type=int, default=int(os.getenv("SSH_LOCAL_PORT", "18080")), help="Loopback port used by the SSH tunnel")
    parser.add_argument("--refresh", type=int, default=int(os.getenv("REFRESH", "15")), help="Device polling interval in seconds")
    parser.add_argument("--detail-refresh", type=int, default=int(os.getenv("DETAIL_REFRESH", "60")), help="Successful model polling interval in seconds")
    parser.add_argument("--disconnect-after", type=int, default=int(os.getenv("DISCONNECT_AFTER", "120")), help="Age in seconds before an inverter or PV Link is disconnected")
    parser.add_argument("--pv-inventory-file", default=os.getenv("PV_INVENTORY_FILE", "/data/pv_inventory.json"), help="Persistent learned PV Link inventory")
    parser.add_argument("--pv-inventory-freeze", action="store_true", default=environment_flag("PV_INVENTORY_FREEZE"), help="Do not add newly observed PV Links")
    discovery = parser.add_mutually_exclusive_group()
    discovery.add_argument("--ha-discovery", dest="ha_discovery", action="store_true")
    discovery.add_argument("--no-ha-discovery", dest="ha_discovery", action="store_false")
    parser.set_defaults(ha_discovery=environment_flag("HA_DISCOVERY_ENABLED", True))
    parser.add_argument("--ha-discovery-prefix", default=os.getenv("HA_DISCOVERY_PREFIX", "homeassistant"), help="Home Assistant MQTT discovery prefix")
    operating_mode = parser.add_mutually_exclusive_group()
    operating_mode.add_argument("--operating-mode-control", dest="operating_mode_control", action="store_true", help="Allow Home Assistant to change approved system operating modes")
    operating_mode.add_argument("--no-operating-mode-control", dest="operating_mode_control", action="store_false")
    parser.set_defaults(operating_mode_control=environment_flag("OPERATING_MODE_CONTROL_ENABLED"))
    parser.add_argument("--web", action="store_true", default=environment_flag("WEB_ENABLED"), help="Enable the authenticated installer web gateway")
    parser.add_argument("--web-write", action="store_true", default=environment_flag("WEB_WRITE_ENABLED"), help="Allow write methods through the web gateway")
    parser.add_argument("--web-listen", default=os.getenv("WEB_LISTEN", "0.0.0.0"), help="Web gateway listen address")
    parser.add_argument("--web-port", type=int, default=int(os.getenv("WEB_PORT", "8000")), help="Web gateway listen port")
    parser.add_argument("--web-user", default=os.getenv("WEB_USERNAME"), help="Web gateway Basic Auth username")
    parser.add_argument("--web-password-file", default=os.getenv("WEB_PASSWORD_FILE"), help="File containing the web gateway password")
    parser.add_argument("ignore", nargs="*", help="Device serials to ignore")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser


def validate_arguments(args):
    for name in ("mqtt_port", "ssh_port", "ssh_local_port", "web_port"):
        value = getattr(args, name)
        if not 1 <= value <= 65535:
            raise ValueError(f"{name.replace('_', '-')} must be between 1 and 65535")
    for name in ("refresh", "detail_refresh", "disconnect_after"):
        if getattr(args, name) < 1:
            raise ValueError(f"{name.replace('_', '-')} must be at least 1")
    if bool(args.user) != bool(args.password):
        raise ValueError("MQTT user and password must be configured together")
    if not args.basetopic.strip("/"):
        raise ValueError("MQTT base topic must not be empty")
    if args.ha_discovery and not args.ha_discovery_prefix.strip("/"):
        raise ValueError("Home Assistant discovery prefix must not be empty")


def main(argv=None):
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logging.critical("Missing runtime dependency: paho-mqtt")
        return 2

    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        validate_arguments(args)
    except (GatewayConfigurationError, ValueError) as error:
        logging.critical("%s", error)
        return 2

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        inventory = PvInventory(args.pv_inventory_file, frozen=args.pv_inventory_freeze)
    except InventoryError as error:
        logging.critical("%s", error)
        return 2
    if inventory.frozen and not inventory.serials:
        logging.warning("PV inventory is frozen but empty; no PV Links will be expected")

    try:
        web_password = None
        if args.web:
            web_password = resolve_web_password(args.web_password_file, os.getenv("WEB_PASSWORD"))
        web_config = WebGatewayConfig(
            enabled=args.web,
            allow_writes=args.web_write,
            listen_address=args.web_listen,
            listen_port=args.web_port,
            username=args.web_user,
            password=web_password,
            upstream_port=args.ssh_local_port,
        )
        web_config.validate()
    except GatewayConfigurationError as error:
        logging.critical("%s", error)
        return 2

    tunnel = SshTunnelSupervisor(SshTunnelConfig(
        hostname=args.hostname,
        private_key=args.idrsa,
        host_fingerprint=args.ssh_host_fingerprint,
        ssh_port=args.ssh_port,
        local_port=args.ssh_local_port,
    ))
    try:
        tunnel.start()
    except TransportConfigurationError as error:
        logging.critical("%s", error)
        return 2

    gateway = InstallerWebGateway(web_config, tunnel)
    try:
        gateway.start()
    except GatewayConfigurationError as error:
        logging.critical("%s", error)
        tunnel.stop()
        return 2

    client_id = args.mqtt_client_id or f"pika2mqtt-{stable_id(args.hostname)}"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    if args.user:
        client.username_pw_set(args.user, args.password)
    else:
        logging.warning("Not using MQTT authentication")
    publisher = MqttBridge(
        client,
        args.basetopic,
        fallback_system_id=args.hostname,
        discovery_enabled=args.ha_discovery,
        discovery_prefix=args.ha_discovery_prefix,
        operating_mode_control_enabled=args.operating_mode_control,
    )
    telemetry = InstallerTelemetry(
        tunnel.base_url,
        inventory,
        ignored=args.ignore,
        transport=tunnel,
        detail_interval=args.detail_refresh,
        disconnect_after=args.disconnect_after,
    )
    monitor = CollectorThread(telemetry, publisher, tunnel, refresh=args.refresh)
    publisher.set_operating_mode_command_handler(monitor.request_operating_mode)
    stop_event = threading.Event()

    def shutdown(signum=None, frame=None):
        if signum is not None:
            logging.info("Received signal %d, shutting down", signum)
        stop_event.set()

    previous_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, shutdown)

    try:
        logging.info("Connecting to MQTT broker %s:%d as %s", args.mqtt, args.mqtt_port, client_id)
        client.connect_async(args.mqtt, args.mqtt_port, 60)
        client.loop_start()
        monitor.start()
        while not stop_event.wait(0.5):
            if tunnel.wait_fatal(0):
                logging.critical("SSH tunnel stopped: %s", tunnel.fatal_error)
                break
    except KeyboardInterrupt:
        shutdown()
    finally:
        monitor.stop()
        if monitor.is_alive():
            monitor.join(timeout=10)
        publisher.shutdown()
        client.loop_stop()
        gateway.stop()
        tunnel.stop()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)

    return 1 if tunnel.fatal_error else 0


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


if __name__ == "__main__":
    sys.exit(main())
