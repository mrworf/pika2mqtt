"""Reliable MQTT publication and Home Assistant device discovery."""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any


LOG = logging.getLogger(__name__)


def stable_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


class MqttBridge:
    QOS = 1

    def __init__(
        self,
        client: Any,
        base_topic: str,
        fallback_system_id: str,
        discovery_enabled: bool = True,
        discovery_prefix: str = "homeassistant",
    ):
        self.client = client
        self.base_topic = base_topic.strip("/")
        self.fallback_system_id = stable_id(fallback_system_id) or "pika"
        self.discovery_enabled = discovery_enabled
        self.discovery_prefix = discovery_prefix.strip("/")
        self.connected = threading.Event()
        self._lock = threading.RLock()
        self._latest: dict[str, Any] | None = None
        self._root_id: str | None = None
        self._discovery_payloads: dict[str, str] = {}
        self.client.will_set(
            self._topic("availability/service"),
            "disconnected",
            qos=self.QOS,
            retain=True,
        )
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.on_connect = self.on_connect
        self.client.on_connect_fail = self.on_connect_fail
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

    def _topic(self, suffix: str) -> str:
        return f"{self.base_topic}/{suffix}"

    @staticmethod
    def _failed_reason(reason_code: Any) -> bool:
        if hasattr(reason_code, "is_failure"):
            return bool(reason_code.is_failure)
        try:
            return int(reason_code) != 0
        except (TypeError, ValueError):
            return True

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        if self._failed_reason(reason_code):
            LOG.error("MQTT connection rejected: %s", reason_code)
            self.connected.clear()
            return
        LOG.info("MQTT broker connected")
        self.connected.set()
        client.subscribe(f"{self.discovery_prefix}/status", qos=self.QOS)
        self._publish(self._topic("availability/service"), "connected")
        self.republish(force_discovery=True)

    def on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=0, properties=None):
        self.connected.clear()
        if self._failed_reason(reason_code):
            LOG.warning("MQTT broker disconnected unexpectedly (%s); reconnecting", reason_code)
        else:
            LOG.info("MQTT broker disconnected")

    def on_connect_fail(self, client, userdata):
        self.connected.clear()
        LOG.warning("MQTT connection attempt failed; automatic retry remains active")

    def on_message(self, client, userdata, message):
        if message.topic != f"{self.discovery_prefix}/status":
            return
        try:
            status = message.payload.decode("utf-8").strip().lower()
        except (AttributeError, UnicodeDecodeError):
            return
        if status == "online":
            LOG.info("Home Assistant birth received; republishing discovery and state")
            self.republish(force_discovery=True)

    def wait_connected(self, timeout: float | None = None) -> bool:
        return self.connected.wait(timeout)

    def _publish(self, topic: str, payload: str) -> Any:
        result = self.client.publish(topic, payload, qos=self.QOS, retain=True)
        if getattr(result, "rc", 0) != 0:
            LOG.error("MQTT publish failed for %s (rc=%s)", topic, result.rc)
        return result

    def publish_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._latest = snapshot
        if self.connected.is_set():
            self.republish()

    def republish(self, force_discovery: bool = False) -> None:
        if not self.connected.is_set():
            return
        with self._lock:
            snapshot = self._latest
            if snapshot is None:
                return
            self._publish_snapshot(snapshot)
            if self.discovery_enabled:
                self._publish_discovery(snapshot, force=force_discovery)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), sort_keys=True)

    def _publish_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._publish(
            self._topic("availability/inverter"),
            "connected" if snapshot["api_connected"] else "disconnected",
        )
        self._publish(self._topic("state/system"), self._json(snapshot["system"]))
        if snapshot.get("inverter") is not None:
            self._publish(self._topic("state/inverter"), self._json(snapshot["inverter"]))
        if snapshot.get("grid") is not None:
            self._publish(self._topic("state/grid"), self._json(snapshot["grid"]))
        for battery in snapshot.get("batteries", []):
            self._publish(
                self._topic(f"state/battery/{battery['serial']}"), self._json(battery)
            )
        for serial, pv in snapshot.get("pv_links", {}).items():
            self._publish(
                self._topic(f"availability/pv/{serial}"),
                "connected" if pv["connected"] else "disconnected",
            )
            self._publish(self._topic(f"state/pv/{serial}"), self._json(pv))

    def _availability(self, include_inverter=True, pv_serial: str | None = None):
        topics = [self._topic("availability/service")]
        if include_inverter:
            topics.append(self._topic("availability/inverter"))
        if pv_serial:
            topics.append(self._topic(f"availability/pv/{pv_serial}"))
        return [
            {
                "topic": topic,
                "payload_available": "connected",
                "payload_not_available": "disconnected",
            }
            for topic in topics
        ]

    def _component(
        self,
        platform: str,
        object_id: str,
        name: str,
        state_topic: str,
        value_template: str,
        availability: list[dict[str, str]],
        **extra,
    ) -> dict[str, Any]:
        component = {
            "platform": platform,
            "name": name,
            "unique_id": object_id,
            "state_topic": state_topic,
            "value_template": value_template,
            "availability": availability,
            "availability_mode": "all",
        }
        component.update({key: value for key, value in extra.items() if value is not None})
        return component

    def _sensor(self, root: str, key: str, name: str, topic: str, **extra):
        object_key = extra.pop("object_key", key)
        return self._component(
            "sensor", f"{root}_{object_key}", name, topic, f"{{{{ value_json.{key} }}}}",
            extra.pop("availability"), **extra
        )

    def _binary(self, root: str, key: str, name: str, topic: str, **extra):
        return self._component(
            "binary_sensor",
            f"{root}_{key}",
            name,
            topic,
            f"{{{{ 'ON' if value_json.{key} else 'OFF' }}}}",
            extra.pop("availability"),
            payload_on="ON",
            payload_off="OFF",
            **extra,
        )

    @staticmethod
    def _device(identifier: str, name: str, model: str, via: str | None = None):
        device = {
            "identifiers": [identifier],
            "name": name,
            "manufacturer": "Generac",
            "model": model,
        }
        if via:
            device["via_device"] = via
        return device

    def _config(self, device: dict[str, Any], components: dict[str, Any]):
        return {
            "device": device,
            "origin": {
                "name": "pika2mqtt",
                "sw_version": "2",
                "support_url": "https://github.com/mrworf/pika2mqtt",
            },
            "components": components,
            "qos": self.QOS,
        }

    def _publish_config(self, object_id: str, config: dict[str, Any], force: bool):
        topic = f"{self.discovery_prefix}/device/{object_id}/config"
        payload = self._json(config)
        if force or self._discovery_payloads.get(topic) != payload:
            self._publish(topic, payload)
            self._discovery_payloads[topic] = payload

    def _publish_discovery(self, snapshot: dict[str, Any], force: bool) -> None:
        inverter = snapshot.get("inverter")
        if self._root_id is None and inverter:
            self._root_id = f"pika2mqtt_{stable_id(inverter['serial'])}"
        if self._root_id is None:
            return
        root = self._root_id
        parent_availability = self._availability()
        service_availability = self._availability(include_inverter=False)
        system_topic = self._topic("state/system")
        inverter_topic = self._topic("state/inverter")
        grid_topic = self._topic("state/grid")
        components = {
            "solar_power": self._sensor(root, "solar_power_w", "Solar power", system_topic, availability=parent_availability, device_class="power", unit_of_measurement="W", state_class="measurement"),
            "learned_strings": self._sensor(root, "learned_string_count", "Learned strings", system_topic, availability=service_availability, entity_category="diagnostic"),
            "connected_strings": self._sensor(root, "connected_string_count", "Connected strings", system_topic, availability=service_availability, entity_category="diagnostic"),
            "disconnected_strings": self._sensor(root, "disconnected_string_count", "Disconnected strings", system_topic, availability=service_availability, entity_category="diagnostic"),
            "faulted_strings": self._sensor(root, "faulted_string_count", "Faulted strings", system_topic, availability=service_availability, entity_category="diagnostic"),
            "unknown_fault_strings": self._sensor(root, "unknown_fault_string_count", "Strings with unknown fault state", system_topic, availability=service_availability, entity_category="diagnostic"),
            "untracked_strings": self._sensor(root, "untracked_pv_link_count", "Untracked PV Links", system_topic, availability=service_availability, entity_category="diagnostic"),
            "any_string_disconnected": self._binary(root, "any_string_disconnected", "String disconnected", system_topic, availability=service_availability, device_class="problem"),
            "any_string_faulted": self._binary(root, "any_string_faulted", "String fault", system_topic, availability=service_availability, device_class="problem"),
            "inverter_power": self._sensor(root, "power_w", "Inverter power", inverter_topic, availability=parent_availability, object_key="inverter_power_w", device_class="power", unit_of_measurement="W", state_class="measurement"),
            "inverter_energy": self._sensor(root, "accumulated_energy_kwh", "Inverter accumulated energy", inverter_topic, availability=parent_availability, device_class="energy", unit_of_measurement="kWh", state_class="total_increasing", entity_category="diagnostic", enabled_by_default=False),
            "inverter_status": self._sensor(root, "status", "Inverter status", inverter_topic, availability=parent_availability),
            "grid_power": self._sensor(root, "power_w", "Grid power", grid_topic, availability=parent_availability, object_key="grid_power_w", device_class="power", unit_of_measurement="W", state_class="measurement"),
            "grid_import_power": self._sensor(root, "import_power_w", "Grid import power", grid_topic, availability=parent_availability, device_class="power", unit_of_measurement="W", state_class="measurement"),
            "grid_export_power": self._sensor(root, "export_power_w", "Grid export power", grid_topic, availability=parent_availability, device_class="power", unit_of_measurement="W", state_class="measurement"),
            "grid_import_energy": self._sensor(root, "import_energy_kwh", "Grid imported energy", grid_topic, availability=parent_availability, device_class="energy", unit_of_measurement="kWh", state_class="total_increasing"),
            "grid_export_energy": self._sensor(root, "export_energy_kwh", "Grid exported energy", grid_topic, availability=parent_availability, device_class="energy", unit_of_measurement="kWh", state_class="total_increasing"),
        }
        components.update(self._raw_components(root, inverter_topic, inverter, parent_availability))
        self._publish_config(
            root,
            self._config(self._device(root, "Generac PWRcell", "PWRcell inverter"), components),
            force,
        )
        for battery in snapshot.get("batteries", []):
            self._publish_battery_discovery(root, battery, force)
        for serial, pv in snapshot.get("pv_links", {}).items():
            self._publish_pv_discovery(root, serial, pv, force)

    def _publish_battery_discovery(self, root: str, battery: dict[str, Any], force: bool):
        serial = battery["serial"]
        child = f"pika2mqtt_battery_{stable_id(serial)}"
        topic = self._topic(f"state/battery/{serial}")
        availability = self._availability()
        specs = [
            ("power_w", "Power", "power", "W", "measurement"),
            ("input_power_w", "Charging power", "power", "W", "measurement"),
            ("output_power_w", "Discharging power", "power", "W", "measurement"),
            ("state_of_charge_percent", "State of charge", "battery", "%", "measurement"),
            ("state_of_health_percent", "State of health", None, "%", "measurement"),
            ("rated_capacity_kwh", "Rated capacity", "energy_storage", "kWh", None),
            ("voltage_v", "Voltage", "voltage", "V", "measurement"),
            ("current_a", "Current", "current", "A", "measurement"),
            ("temperature_c", "Temperature", "temperature", "°C", "measurement"),
            ("status", "Status", None, None, None),
        ]
        components = {
            key: self._sensor(child, key, name, topic, availability=availability, device_class=device_class, unit_of_measurement=unit, state_class=state_class)
            for key, name, device_class, unit, state_class in specs
        }
        components.update(self._raw_components(child, topic, battery, availability))
        self._publish_config(child, self._config(self._device(child, "PWRcell battery", "PWRcell battery", root), components), force)

    def _publish_pv_discovery(self, root: str, serial: str, pv: dict[str, Any], force: bool):
        child = f"pika2mqtt_pv_{stable_id(serial)}"
        topic = self._topic(f"state/pv/{serial}")
        connected_availability = self._availability()
        measurement_availability = self._availability(pv_serial=serial)
        components = {
            "disconnected": self._component("binary_sensor", f"{child}_disconnected", "Disconnected", topic, "{{ 'OFF' if value_json.connected else 'ON' }}", connected_availability, payload_on="ON", payload_off="OFF", device_class="problem"),
            "fault": self._component("binary_sensor", f"{child}_fault", "Fault", topic, "{{ 'ON' if value_json.fault == true else ('OFF' if value_json.fault == false else 'UNKNOWN') }}", measurement_availability, payload_on="ON", payload_off="OFF", device_class="problem"),
            "fault_summary": self._sensor(child, "fault_summary", "Fault summary", topic, availability=measurement_availability, entity_category="diagnostic"),
            "power": self._sensor(child, "power_w", "Power", topic, availability=measurement_availability, device_class="power", unit_of_measurement="W", state_class="measurement"),
            "input_voltage": self._sensor(child, "input_voltage_v", "Input voltage", topic, availability=measurement_availability, device_class="voltage", unit_of_measurement="V", state_class="measurement"),
            "input_current": self._sensor(child, "input_current_a", "Input current", topic, availability=measurement_availability, device_class="current", unit_of_measurement="A", state_class="measurement"),
            "energy": self._sensor(child, "accumulated_energy_kwh", "Accumulated energy", topic, availability=measurement_availability, device_class="energy", unit_of_measurement="kWh", state_class="total_increasing"),
            "status": self._sensor(child, "status", "Status", topic, availability=measurement_availability),
            "enabled": self._binary(child, "enabled", "Enabled", topic, availability=measurement_availability, entity_category="diagnostic"),
            "last_heard": self._sensor(child, "last_heard_seconds", "Last heard age", topic, availability=connected_availability, device_class="duration", unit_of_measurement="s", state_class="measurement", entity_category="diagnostic"),
            "snaprs_installed": self._sensor(child, "snaprs_installed", "SnapRS installed", topic, availability=measurement_availability, entity_category="diagnostic"),
            "snaprs_detected": self._sensor(child, "snaprs_detected", "SnapRS detected", topic, availability=measurement_availability, entity_category="diagnostic"),
            "pvrss_self_test": self._sensor(child, "pvrss_self_test", "PVRSS self-test", topic, availability=measurement_availability, entity_category="diagnostic"),
            "error_word": self._sensor(child, "error_word", "Error word", topic, availability=measurement_availability, entity_category="diagnostic", enabled_by_default=False),
            "status_code": self._sensor(child, "status_code", "Status code", topic, availability=measurement_availability, entity_category="diagnostic", enabled_by_default=False),
        }
        # Every scalar returned by the inverter's model endpoints remains
        # available to Home Assistant without making the default device noisy.
        # Normalized entities above are the stable public interface.
        components.update(self._raw_components(child, topic, pv, measurement_availability))
        self._publish_config(child, self._config(self._device(child, f"PV Link {serial}", "PV Link", root), components), force)

    def _raw_components(self, root, topic, state, availability):
        components = {}
        for model, payload in (state or {}).get("raw_models", {}).items():
            fixed = payload.get("fixed", {}) if isinstance(payload, dict) else {}
            if not isinstance(fixed, dict):
                continue
            for field, value in fixed.items():
                if value is None or not isinstance(value, (str, int, float, bool)):
                    continue
                key = stable_id(f"raw_{model}_{field}")
                components[key] = self._component(
                    "sensor",
                    f"{root}_{key}",
                    f"{model} {field}",
                    topic,
                    "{{ value_json.raw_models[" + json.dumps(model) + "].fixed[" + json.dumps(field) + "] }}",
                    availability,
                    entity_category="diagnostic",
                    enabled_by_default=False,
                )
        return components

    def shutdown(self) -> None:
        if self.connected.is_set():
            result = self._publish(self._topic("availability/service"), "disconnected")
            try:
                result.wait_for_publish(timeout=2)
            except (AttributeError, RuntimeError, ValueError):
                pass
        self.connected.clear()
        self.client.disconnect()
