"""Reliable MQTT publication and Home Assistant device discovery."""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any
from register_definitions import atomic_write

from telemetry import SYSTEM_OPERATING_MODE_COMMANDS


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
        operating_mode_control_enabled: bool = False,
        diagnostic_manifest_file: str | None = None,
    ):
        self.client = client
        self.base_topic = base_topic.strip("/")
        self.fallback_system_id = stable_id(fallback_system_id) or "pika"
        self.discovery_enabled = discovery_enabled
        self.discovery_prefix = discovery_prefix.strip("/")
        self.operating_mode_control_enabled = operating_mode_control_enabled
        self.operating_mode_command_handler = None
        self.pv_link_command_handler = None
        self.connected = threading.Event()
        self._lock = threading.RLock()
        self._latest: dict[str, Any] | None = None
        self._root_id: str | None = None
        self._root_serial: str | None = None
        self._discovery_payloads: dict[str, str] = {}
        self._removed_boolean_components: dict[str, set[str]] = {}
        self._known_battery_modules: dict[str, set[int]] = {}
        self._manifest_file = diagnostic_manifest_file
        self._diagnostic_manifest = {}
        if diagnostic_manifest_file:
            try:
                manifest = json.loads(Path(diagnostic_manifest_file).read_text())
                if not isinstance(manifest, dict) or any(not isinstance(key, str) or not isinstance(values, dict) or not isinstance(values.get("components"), dict) or not isinstance(values.get("checksum"), str) for key, values in manifest.items()):
                    raise ValueError("invalid manifest")
                for values in manifest.values():
                    legacy = values.get("legacy_boolean_components", [])
                    if not isinstance(legacy, list) or any(not isinstance(key, str) for key in legacy):
                        raise ValueError("invalid legacy boolean component list")
                self._diagnostic_manifest = manifest
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as error:
                LOG.warning("Cannot load register discovery manifest: %s", error)
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
        if self.operating_mode_control_enabled:
            client.subscribe(self._topic("command/system_operating_mode"), qos=self.QOS)
            client.subscribe(self._topic("command/pv/+/enabled"), qos=self.QOS)
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
        if message.topic == f"{self.discovery_prefix}/status":
            try:
                status = message.payload.decode("utf-8").strip().lower()
            except (AttributeError, UnicodeDecodeError):
                return
            if status == "online":
                LOG.info("Home Assistant birth received; republishing discovery and state")
                self.republish(force_discovery=True)
            return
        if message.topic == self._topic("command/system_operating_mode"):
            self._handle_operating_mode_command(message)
            return
        match = re.fullmatch(
            re.escape(self._topic("command/pv/"))
            + r"([0-9A-Fa-f]{12})/enabled",
            message.topic,
        )
        if match:
            self._handle_pv_link_command(message, match.group(1).upper())

    def _handle_operating_mode_command(self, message) -> None:
        if not self.operating_mode_control_enabled:
            LOG.warning("Ignoring operating mode command while control is disabled")
            return
        if getattr(message, "retain", False):
            LOG.warning("Ignoring retained operating mode command")
            return
        try:
            label = message.payload.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            LOG.warning("Ignoring malformed operating mode command")
            return
        code = SYSTEM_OPERATING_MODE_COMMANDS.get(label)
        if code is None:
            LOG.warning("Ignoring unknown operating mode command: %r", label)
            return
        if self.operating_mode_command_handler is None:
            LOG.error("Cannot process operating mode command: no handler is configured")
            return
        LOG.info("Accepted operating mode command: %s", label)
        self.operating_mode_command_handler(label, code)

    def _handle_pv_link_command(self, message, serial: str) -> None:
        if not self.operating_mode_control_enabled:
            LOG.warning("Ignoring PV Link command while control is disabled")
            return
        if getattr(message, "retain", False):
            LOG.warning("Ignoring retained PV Link command for %s", serial)
            return
        payload = getattr(message, "payload", None)
        if payload == b"ON":
            enabled = True
        elif payload == b"OFF":
            enabled = False
        else:
            LOG.warning("Ignoring invalid PV Link command for %s: %r", serial, payload)
            return
        if self.pv_link_command_handler is None:
            LOG.error("Cannot process PV Link command: no handler is configured")
            return
        LOG.info(
            "Accepted PV Link command: %s %s",
            serial,
            "enabled" if enabled else "disabled",
        )
        self.pv_link_command_handler(serial, enabled)

    def set_operating_mode_command_handler(self, handler) -> None:
        self.operating_mode_command_handler = handler

    def set_pv_link_command_handler(self, handler) -> None:
        self.pv_link_command_handler = handler

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
        self._publish(
            self._topic("availability/power/solar"),
            "available" if "solar_power_w" in snapshot["system"] else "unavailable",
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
        for serial, group in snapshot.get("battery_modules", {}).items():
            model_fresh = bool(group.get("endpoint_health", {}).get("fresh"))
            self._publish(
                self._topic(f"availability/battery/{serial}/modules"),
                "available" if model_fresh else "unavailable",
            )
            modules = group.get("modules", {})
            modules = modules if isinstance(modules, dict) else {}
            known = self._known_battery_modules.setdefault(serial, set())
            for key in modules:
                try:
                    index = int(key)
                except (TypeError, ValueError):
                    continue
                if index > 0:
                    known.add(index)
            for index in sorted(known):
                module = modules.get(str(index))
                present = bool(
                    model_fresh
                    and isinstance(module, dict)
                    and module.get("present")
                )
                self._publish(
                    self._topic(
                        f"availability/battery/{serial}/module/{index}"
                    ),
                    "available" if present else "unavailable",
                )
                if model_fresh and isinstance(module, dict):
                    self._publish(
                        self._topic(f"state/battery/{serial}/module/{index}"),
                        self._json(module),
                    )
        for serial, pv in snapshot.get("pv_links", {}).items():
            self._publish(
                self._topic(f"availability/pv/{serial}"),
                "connected" if pv["connected"] else "disconnected",
            )
            self._publish(
                self._topic(f"availability/power/pv/{serial}"),
                "available" if "power_w" in pv else "unavailable",
            )
            self._publish(
                self._topic(f"availability/pv/{serial}/enabled"),
                "available" if pv.get("enabled") is not None else "unavailable",
            )
            self._publish(
                self._topic(f"availability/pv/{serial}/core"),
                "available" if pv.get("core_state_available") else "unavailable",
            )
            self._publish(
                self._topic(f"availability/pv/{serial}/fault"),
                "available" if pv.get("fault") is not None else "unavailable",
            )
            if self.operating_mode_control_enabled:
                self._publish(
                    self._topic(f"availability/control/pv/{serial}"),
                    "available"
                    if pv.get("enable_control_available") is True
                    else "unavailable",
                )
            self._publish(self._topic(f"state/pv/{serial}"), self._json(pv))
        devices = [snapshot.get("inverter"), *snapshot.get("batteries", [])]
        devices.extend(snapshot.get("pv_links", {}).values())
        for device in devices:
            if not device:
                continue
            for model, health in device.get("endpoint_health", {}).items():
                self._publish(
                    self._topic(f"availability/model/{device['serial']}/{model}"),
                    "available" if health.get("fresh") else "unavailable",
                )
        diagnostic_devices = list(devices)
        for group in snapshot.get("battery_modules", {}).values():
            diagnostic_devices.extend(group.get("modules", {}).values())
        health = snapshot["system"].get("register_definitions", {})
        self._publish(self._topic("availability/definitions"), "available" if health.get("available") else "unavailable")
        observed_register_topics = set()
        for device in diagnostic_devices:
            if not device or "serial" not in device:
                continue
            serial = device["serial"]
            for key, register in device.get("decoded_registers", {}).items():
                topic = self._topic(f"availability/register/{serial}/{stable_id(key)}")
                observed_register_topics.add(topic)
                self._publish(topic, "available" if register["available"] else "unavailable")
            topic = self._topic(f"availability/register/{serial}/errors")
            observed_register_topics.add(topic)
            self._publish(topic, "available" if device.get("active_error_count") is not None else "unavailable")
        for manifest in self._diagnostic_manifest.values():
            for component in manifest["components"].values():
                for entry in component.get("availability", []):
                    topic = entry["topic"]
                    if topic.startswith(self._topic("availability/register/")) and topic not in observed_register_topics:
                        self._publish(topic, "unavailable")

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

    def _power_availability(self, suffix: str) -> dict[str, str]:
        return {
            "topic": self._topic(f"availability/power/{suffix}"),
            "payload_available": "available",
            "payload_not_available": "unavailable",
        }

    def _model_availability(
        self,
        availability: list[dict[str, str]],
        serial: str,
        *models: str,
    ) -> list[dict[str, str]]:
        return availability + [
            {
                "topic": self._topic(f"availability/model/{serial}/{model}"),
                "payload_available": "available",
                "payload_not_available": "unavailable",
            }
            for model in models
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

    @staticmethod
    def _truth_components(components):
        """Normalize current and persisted legacy boolean discovery definitions.

        Legacy templates are generated locally, not taken from firmware. Firmware
        still owns each symbol, bit position, label, and severity attribute.
        """
        normalized, legacy = {}, set()
        for key, component in components.items():
            if component.get("platform") != "binary_sensor":
                normalized[key] = component
                continue
            match = re.fullmatch(r"\{\{ '(ON|OFF)' if (.+) else '(ON|OFF)' \}\}", component["value_template"])
            if not match or match[1] == match[3]:
                raise ValueError(f"Unsupported boolean discovery template: {key}")
            expression = match[2]
            source = expression.split(" in ", 1)[-1] if " in " in expression else expression.removesuffix(" == true")
            # Guard every ancestor, including absent decoded_registers entries.
            path, guards = "value_json", []
            for segment in re.findall(r'\.[A-Za-z_]\w*|\["(?:\\.|[^"\\])*"\]', source.removeprefix("value_json")):
                path += segment
                guards.append(f"{path} is defined and {path} is not none")
            if " in " in expression:
                guards.append(f"{source} is sequence and {source} is not string and {source} is not mapping")
            else:
                guards.append(f"{source} is boolean")
            true_value, false_value = ("True", "False") if match[1] == "ON" else ("False", "True")
            template = "{% if " + " and ".join(guards) + " %}{{ '" + true_value + "' if " + expression + " else '" + false_value + "' }}{% else %}unknown{% endif %}"
            truth = {name: value for name, value in component.items()
                     if name not in ("payload_on", "payload_off", "device_class")}
            truth.update(platform="sensor", unique_id=component["unique_id"] + "_truth", value_template=template)
            normalized[key + "_truth"] = truth
            legacy.add(key)
        return normalized, legacy

    def _publish_config(self, object_id: str, config: dict[str, Any], force: bool):
        topic = f"{self.discovery_prefix}/device/{object_id}/config"
        health = (self._latest or {}).get("system", {}).get("register_definitions", {})
        valid = bool(health.get("available"))
        checksum = health.get("checksum") or ""
        previous = self._diagnostic_manifest.get(topic, {"components": {}, "checksum": ""})
        old_components, old_boolean_keys = self._truth_components(previous["components"])
        components, boolean_keys = self._truth_components(config["components"])
        config = {**config, "components": components}
        if not valid or previous["checksum"] == checksum:
            # Missing observations do not imply a register was removed from firmware.
            config = {**config, "components": {**old_components, **config["components"]}}
        legacy_keys = (boolean_keys | old_boolean_keys
                       | set(previous.get("legacy_boolean_components", []))
                       | {key.removesuffix("_truth") for key in config["components"] if key.endswith("_truth")})
        # Retain tombstones in the final message too: HA may be offline during
        # the intermediate cleanup publication. Persist their identities across
        # restarts/firmware changes, independently of firmware pruning.
        config = {**config, "components": {
            **config["components"],
            **{key: {"platform": "binary_sensor"} for key in legacy_keys},
        }}
        payload = self._json(config)
        if force or self._discovery_payloads.get(topic) != payload:
            generated = {key: value for key, value in config["components"].items()
                         if key.startswith("decoded_") and key not in legacy_keys}
            removed_booleans = self._removed_boolean_components.setdefault(topic, set())
            legacy = legacy_keys - removed_booleans
            if legacy:
                # Distinct replacement keys make cleanup safe on every restart.
                # HA requires the original platform even for removal entries.
                cleanup = {**config, "components": {
                    **{key: value for key, value in config["components"].items() if not key.endswith("_truth")},
                    **{key: {"platform": "binary_sensor"} for key in legacy},
                }}
                if getattr(self._publish(topic, self._json(cleanup)), "rc", 0):
                    return
                removed_booleans.update(legacy)
            # Only validated definitions justify deleting firmware-generated entities.
            if valid:
                removed = set(old_components) - set(generated)
                if removed:
                    removal = {**config, "components": {
                        **config["components"],
                        **{key: {"platform": old_components[key]["platform"]} for key in removed},
                    }}
                    if getattr(self._publish(topic, self._json(removal)), "rc", 0):
                        return
            if getattr(self._publish(topic, payload), "rc", 0):
                return
            self._discovery_payloads[topic] = payload
            manifest = {"checksum": checksum if valid else previous["checksum"], "components": generated,
                        "legacy_boolean_components": sorted(legacy_keys)}
            if previous != manifest:
                self._diagnostic_manifest[topic] = manifest
                if self._manifest_file:
                    try:
                        atomic_write(self._manifest_file, self._json(self._diagnostic_manifest))
                    except OSError as error:
                        LOG.warning("Cannot persist register discovery manifest: %s", error)

    def _diagnostic_components(self, root, topic, state, availability):
        components = {}
        if not state or "decoded_registers" not in state:
            return components
        serial = state["serial"]
        for key, register in state["decoded_registers"].items():
            register_availability = availability + [{"topic": self._topic(f"availability/register/{serial}/{stable_id(key)}"), "payload_available": "available", "payload_not_available": "unavailable"}]
            access = "value_json.decoded_registers[" + json.dumps(key) + "]"
            attributes = {"json_attributes_topic": topic, "json_attributes_template": "{{ {'raw': " + access + ".raw, 'symbol': " + access + ".symbol, 'active_symbols': " + access + ".active_symbols, 'description': " + access + ".description, 'unknown_mask': " + access + ".unknown_mask, 'definition_checksum': " + access + ".checksum, 'supported_states': " + access + ".supported_states} | tojson }}"}
            if register["kind"].startswith("enum"):
                component_key = "decoded_" + stable_id(key)
                name = "Last event" if register["register"] == "Ev" and register["model"] == "REbus_status" else f"{register['model']} {register['label']}"
                components[component_key] = self._component("sensor", f"{root}_{component_key}", name, topic, "{{ " + access + ".state }}", register_availability, entity_category="diagnostic", enabled_by_default=(name == "Last event"), **attributes)
            else:
                for bit, metadata in register["flags"].items():
                    component_key = "decoded_" + stable_id(key) + f"_bit_{bit}"
                    name = f"{register['model']} {register['register']} {metadata['label']}"
                    flag_attributes = {**attributes, "json_attributes_template": "{{ {'raw': " + access + ".raw, 'symbol': " + json.dumps(metadata["symbol"]) + ", 'description': " + json.dumps(metadata["description"]) + ", 'policy_classification': " + json.dumps(metadata["severity"]) + ", 'definition_checksum': " + access + ".checksum} | tojson }}"}
                    components[component_key] = self._component("binary_sensor", f"{root}_{component_key}", name, topic, "{{ 'ON' if " + json.dumps(metadata["symbol"]) + " in " + access + ".active_symbols else 'OFF' }}", register_availability, payload_on="ON", payload_off="OFF", device_class="problem" if metadata["severity"] in ("warning", "error") else None, entity_category="diagnostic", enabled_by_default=False, **flag_attributes)
        if "active_error_count" in state:
            key = "decoded_active_error_count"
            components[key] = self._sensor(root, "active_error_count", "Active error count", topic, object_key=key,
                availability=availability + [{"topic": self._topic(f"availability/register/{serial}/errors"), "payload_available": "available", "payload_not_available": "unavailable"}],
                entity_category="diagnostic", json_attributes_topic=topic,
                json_attributes_template="{{ {'active_errors': value_json.active_errors, 'coverage_complete': value_json.error_coverage_complete} | tojson }}")
        return components

    def _status_attributes(self, components, topic, state):
        if not state or "decoded_registers" not in state:
            return
        registers = state["decoded_registers"]
        status_key = "REbus_dir.repeating.St" if "REbus_dir.repeating.St" in registers else "REbus_status.fixed.St"
        for component_key, key in (("inverter_status", status_key), ("status", status_key), ("status_code", status_key), ("pvrss_self_test", "pvrss_telemetry.fixed.SelfTestResults")):
            component = components.get(component_key)
            if component is None:
                continue
            component["availability"] = component["availability"] + [{"topic": self._topic(f"availability/register/{state['serial']}/{stable_id(key)}"), "payload_available": "available", "payload_not_available": "unavailable"}]
            access = "value_json.decoded_registers[" + json.dumps(key) + "]"
            component.update(json_attributes_topic=topic, json_attributes_template="{{ {'raw': " + access + ".raw, 'symbol': " + access + ".symbol, 'description': " + access + ".description, 'supported_states': " + access + ".supported_states} | tojson }}")

    def _publish_discovery(self, snapshot: dict[str, Any], force: bool) -> None:
        inverter = snapshot.get("inverter")
        if self._root_id is None and inverter:
            self._root_id = f"pika2mqtt_{stable_id(inverter['serial'])}"
            self._root_serial = inverter["serial"]
        if self._root_id is None:
            return
        root = self._root_id
        parent_availability = self._availability()
        service_availability = self._availability(include_inverter=False)
        system_topic = self._topic("state/system")
        inverter_topic = self._topic("state/inverter")
        grid_topic = self._topic("state/grid")
        inverter_serial = inverter["serial"] if inverter else self._root_serial
        rebus_availability = self._model_availability(
            parent_availability, inverter_serial, "REbus_status"
        )
        inverter_status_availability = self._model_availability(
            parent_availability, inverter_serial, "inverter_status"
        )
        expansion_availability = self._model_availability(
            parent_availability, inverter_serial, "REbus_exp"
        )
        components = {
            "solar_power": self._sensor(root, "solar_power_w", "Solar power", system_topic, availability=parent_availability + [self._power_availability("solar")], device_class="power", unit_of_measurement="W", state_class="measurement"),
            "learned_strings": self._sensor(root, "learned_string_count", "Learned strings", system_topic, availability=service_availability, entity_category="diagnostic"),
            "connected_strings": self._sensor(root, "connected_string_count", "Connected strings", system_topic, availability=service_availability, entity_category="diagnostic"),
            "disconnected_strings": self._sensor(root, "disconnected_string_count", "Disconnected strings", system_topic, availability=service_availability, entity_category="diagnostic"),
            "faulted_strings": self._sensor(root, "faulted_string_count", "Faulted strings", system_topic, availability=service_availability, entity_category="diagnostic"),
            "unknown_fault_strings": self._sensor(root, "unknown_fault_string_count", "Strings with unknown fault state", system_topic, availability=service_availability, entity_category="diagnostic"),
            "untracked_strings": self._sensor(root, "untracked_pv_link_count", "Untracked PV Links", system_topic, availability=service_availability, entity_category="diagnostic"),
            "any_string_disconnected": self._binary(root, "any_string_disconnected", "String disconnected", system_topic, availability=service_availability, device_class="problem"),
            "any_string_faulted": self._binary(root, "any_string_faulted", "String fault", system_topic, availability=service_availability, device_class="problem"),
            "inverter_power": self._sensor(root, "power_w", "Inverter power", inverter_topic, availability=parent_availability, object_key="inverter_power_w", device_class="power", unit_of_measurement="W", state_class="measurement"),
            "inverter_energy": self._sensor(root, "accumulated_energy_kwh", "Inverter accumulated energy", inverter_topic, availability=rebus_availability, device_class="energy", unit_of_measurement="kWh", state_class="total_increasing", entity_category="diagnostic", enabled_by_default=False),
            "inverter_status": self._sensor(root, "status", "Inverter status", inverter_topic, availability=rebus_availability),
            "system_operating_mode": self._sensor(root, "system_operating_mode", "System Operating Mode", inverter_topic, availability=inverter_status_availability),
            "grid_power": self._sensor(root, "power_w", "Grid power", grid_topic, availability=inverter_status_availability, object_key="grid_power_w", device_class="power", unit_of_measurement="W", state_class="measurement"),
            "grid_import_power": self._sensor(root, "import_power_w", "Grid import power", grid_topic, availability=inverter_status_availability, device_class="power", unit_of_measurement="W", state_class="measurement"),
            "grid_export_power": self._sensor(root, "export_power_w", "Grid export power", grid_topic, availability=inverter_status_availability, device_class="power", unit_of_measurement="W", state_class="measurement"),
            "grid_import_energy": self._sensor(root, "import_energy_kwh", "Grid imported energy", grid_topic, availability=expansion_availability, device_class="energy", unit_of_measurement="kWh", state_class="total_increasing"),
            "grid_export_energy": self._sensor(root, "export_energy_kwh", "Grid exported energy", grid_topic, availability=expansion_availability, device_class="energy", unit_of_measurement="kWh", state_class="total_increasing"),
        }
        if self.operating_mode_control_enabled:
            components["system_operating_mode_control"] = self._component(
                "select",
                f"{root}_system_operating_mode_control",
                "System Operating Mode Control",
                inverter_topic,
                "{{ value_json.system_operating_mode }}",
                inverter_status_availability,
                command_topic=self._topic("command/system_operating_mode"),
                options=list(SYSTEM_OPERATING_MODE_COMMANDS),
                optimistic=False,
                retain=False,
            )
        components.update(
            self._raw_components(
                root,
                inverter_topic,
                inverter,
                parent_availability,
                inverter_serial,
            )
        )
        components.update(self._diagnostic_components(root, inverter_topic, inverter, parent_availability))
        self._status_attributes(components, inverter_topic, inverter)
        if "register_definitions" in snapshot["system"]:
            components["decoded_definitions_available"] = self._component("binary_sensor", f"{root}_decoded_definitions_available", "Register definitions available", system_topic, "{{ 'ON' if value_json.register_definitions.available else 'OFF' }}", service_availability, payload_on="ON", payload_off="OFF", entity_category="diagnostic", json_attributes_topic=system_topic, json_attributes_template="{{ value_json.register_definitions | tojson }}")
        self._publish_config(
            root,
            self._config(self._device(root, "Generac PWRcell", "PWRcell inverter"), components),
            force,
        )
        for battery in snapshot.get("batteries", []):
            self._publish_battery_discovery(root, battery, force)
        for serial, group in snapshot.get("battery_modules", {}).items():
            modules = group.get("modules", {})
            if not isinstance(modules, dict):
                continue
            for key, module in modules.items():
                try:
                    index = int(key)
                except (TypeError, ValueError):
                    continue
                if index > 0 and isinstance(module, dict):
                    self._publish_battery_module_discovery(
                        serial, index, force
                    )
        for serial, pv in snapshot.get("pv_links", {}).items():
            self._publish_pv_discovery(root, serial, pv, force)

    def _publish_battery_discovery(self, root: str, battery: dict[str, Any], force: bool):
        serial = battery["serial"]
        child = f"pika2mqtt_battery_{stable_id(serial)}"
        topic = self._topic(f"state/battery/{serial}")
        availability = self._availability()
        specs = [
            ("power_w", "Power", "power", "W", "measurement", None),
            ("input_power_w", "Charging power", "power", "W", "measurement", None),
            ("output_power_w", "Discharging power", "power", "W", "measurement", None),
            ("state_of_charge_percent", "State of charge", "battery", "%", "measurement", None),
            ("state_of_health_percent", "State of health", None, "%", "measurement", "battery"),
            ("rated_capacity_kwh", "Rated capacity", "energy_storage", "kWh", None, "battery"),
            ("voltage_v", "Voltage", "voltage", "V", "measurement", "REbus_status"),
            ("current_a", "Current", "current", "A", "measurement", "REbus_status"),
            ("temperature_c", "Temperature", "temperature", "°C", "measurement", "REbus_status"),
            ("status", "Status", None, None, None, "REbus_status"),
        ]
        components = {
            key: self._sensor(
                child,
                key,
                name,
                topic,
                availability=(
                    self._model_availability(availability, serial, model)
                    if model
                    else availability
                ),
                device_class=device_class,
                unit_of_measurement=unit,
                state_class=state_class,
            )
            for key, name, device_class, unit, state_class, model in specs
        }
        components.update(
            self._raw_components(child, topic, battery, availability, serial)
        )
        components.update(self._diagnostic_components(child, topic, battery, availability))
        self._status_attributes(components, topic, battery)
        self._publish_config(child, self._config(self._device(child, "PWRcell battery", "PWRcell battery", root), components), force)

    def _publish_battery_module_discovery(
        self, serial: str, index: int, force: bool
    ) -> None:
        battery = f"pika2mqtt_battery_{stable_id(serial)}"
        child = f"{battery}_module_{index}"
        topic = self._topic(f"state/battery/{serial}/module/{index}")
        availability = self._availability() + [
            {
                "topic": self._topic(
                    f"availability/battery/{serial}/modules"
                ),
                "payload_available": "available",
                "payload_not_available": "unavailable",
            },
            {
                "topic": self._topic(
                    f"availability/battery/{serial}/module/{index}"
                ),
                "payload_available": "available",
                "payload_not_available": "unavailable",
            },
        ]
        specs = [
            ("state_of_charge_percent", "State of charge", "battery", "%"),
            ("state_of_health_percent", "State of health", None, "%"),
        ]
        components = {
            key: self._sensor(
                child,
                key,
                name,
                topic,
                availability=availability,
                device_class=device_class,
                unit_of_measurement=unit,
                state_class="measurement",
            )
            for key, name, device_class, unit in specs
        }
        diagnostics = [
            ("cell_count", "Cell count", None, None),
            ("minimum_cell_voltage_v", "Minimum cell voltage", "voltage", "V"),
            ("maximum_cell_voltage_v", "Maximum cell voltage", "voltage", "V"),
            ("average_cell_voltage_v", "Average cell voltage", "voltage", "V"),
            ("minimum_cell_temperature_c", "Minimum cell temperature", "temperature", "°C"),
            ("maximum_cell_temperature_c", "Maximum cell temperature", "temperature", "°C"),
            ("average_cell_temperature_c", "Average cell temperature", "temperature", "°C"),
        ]
        components.update({
            key: self._sensor(
                child,
                key,
                name,
                topic,
                availability=availability,
                device_class=device_class,
                unit_of_measurement=unit,
                state_class="measurement",
                entity_category="diagnostic",
                enabled_by_default=False,
            )
            for key, name, device_class, unit in diagnostics
        })
        module = (self._latest or {}).get("battery_modules", {}).get(serial, {}).get("modules", {}).get(str(index))
        components.update(self._diagnostic_components(child, topic, module, availability))
        self._publish_config(
            child,
            self._config(
                self._device(
                    child,
                    f"PWRcell Battery Module {index}",
                    "PWRcell battery module",
                    battery,
                ),
                components,
            ),
            force,
        )

    def _publish_pv_discovery(self, root: str, serial: str, pv: dict[str, Any], force: bool):
        child = f"pika2mqtt_pv_{stable_id(serial)}"
        topic = self._topic(f"state/pv/{serial}")
        connected_availability = self._availability()
        measurement_availability = self._availability(pv_serial=serial)
        power_availability = measurement_availability + [self._power_availability(f"pv/{serial}")]
        rebus_availability = self._model_availability(
            measurement_availability, serial, "REbus_status"
        )
        pvlink_availability = self._model_availability(
            measurement_availability, serial, "pvlink_status"
        )
        core_availability = measurement_availability + [
            {
                "topic": self._topic(f"availability/pv/{serial}/core"),
                "payload_available": "available",
                "payload_not_available": "unavailable",
            }
        ]
        fault_availability = measurement_availability + [
            {
                "topic": self._topic(f"availability/pv/{serial}/fault"),
                "payload_available": "available",
                "payload_not_available": "unavailable",
            }
        ]
        enabled_availability = measurement_availability + [
            {
                "topic": self._topic(f"availability/pv/{serial}/enabled"),
                "payload_available": "available",
                "payload_not_available": "unavailable",
            }
        ]
        pvrss_availability = self._model_availability(
            measurement_availability, serial, "pvrss_telemetry"
        )
        components = {
            "disconnected": self._component("binary_sensor", f"{child}_disconnected", "Communication lost", topic, "{{ 'OFF' if value_json.connected else 'ON' }}", connected_availability, payload_on="ON", payload_off="OFF", device_class="problem"),
            "fault": self._component("binary_sensor", f"{child}_fault", "Fault", topic, "{{ 'ON' if value_json.fault == true else 'OFF' }}", fault_availability, payload_on="ON", payload_off="OFF", device_class="problem"),
            "fault_summary": self._sensor(child, "fault_summary", "Fault summary", topic, availability=fault_availability, entity_category="diagnostic"),
            "detailed_fault_data_unavailable": self._component("binary_sensor", f"{child}_detailed_fault_data_unavailable", "Detailed fault data unavailable", topic, "{{ 'OFF' if value_json.detailed_fault_coverage else 'ON' }}", measurement_availability, payload_on="ON", payload_off="OFF", device_class="problem", entity_category="diagnostic", enabled_by_default=False),
            "power": self._sensor(child, "power_w", "Power", topic, availability=power_availability, device_class="power", unit_of_measurement="W", state_class="measurement"),
            "input_voltage": self._sensor(child, "input_voltage_v", "Input voltage", topic, availability=pvlink_availability, device_class="voltage", unit_of_measurement="V", state_class="measurement"),
            "input_current": self._sensor(child, "input_current_a", "Input current", topic, availability=pvlink_availability, device_class="current", unit_of_measurement="A", state_class="measurement"),
            "energy": self._sensor(child, "accumulated_energy_kwh", "Accumulated energy", topic, availability=rebus_availability, device_class="energy", unit_of_measurement="kWh", state_class="total_increasing"),
            "status": self._sensor(child, "status", "Status", topic, availability=core_availability),
            "voltage": self._sensor(child, "voltage_v", "REbus voltage", topic, availability=core_availability, device_class="voltage", unit_of_measurement="V", state_class="measurement"),
            "current": self._sensor(child, "current_a", "REbus current", topic, availability=core_availability, device_class="current", unit_of_measurement="A", state_class="measurement"),
            "temperature": self._sensor(child, "temperature_c", "Temperature", topic, availability=core_availability, device_class="temperature", unit_of_measurement="°C", state_class="measurement"),
            "enabled": self._binary(child, "enabled", "Enabled", topic, availability=enabled_availability, entity_category="diagnostic"),
            "last_heard": self._sensor(child, "last_heard_seconds", "Last heard age", topic, availability=connected_availability, device_class="duration", unit_of_measurement="s", state_class="measurement", entity_category="diagnostic"),
            "snaprs_installed": self._sensor(child, "snaprs_installed", "SnapRS installed", topic, availability=pvrss_availability, entity_category="diagnostic"),
            "snaprs_detected": self._sensor(child, "snaprs_detected", "SnapRS detected", topic, availability=pvrss_availability, entity_category="diagnostic"),
            "pvrss_self_test": self._sensor(child, "pvrss_self_test", "PVRSS self-test", topic, availability=pvrss_availability, entity_category="diagnostic"),
            "error_word": self._sensor(child, "error_word", "Error word", topic, availability=pvlink_availability, entity_category="diagnostic", enabled_by_default=False),
            "status_code": self._sensor(child, "status_code", "Status code", topic, availability=core_availability, entity_category="diagnostic", enabled_by_default=False),
        }
        if self.operating_mode_control_enabled:
            control_availability = measurement_availability + [
                {
                    "topic": self._topic(f"availability/control/pv/{serial}"),
                    "payload_available": "available",
                    "payload_not_available": "unavailable",
                }
            ]
            components["enabled_control"] = self._component(
                "switch",
                f"{child}_enabled_control",
                "Enabled Control",
                topic,
                "{{ 'ON' if value_json.enabled else 'OFF' }}",
                control_availability,
                command_topic=self._topic(f"command/pv/{serial}/enabled"),
                payload_on="ON",
                payload_off="OFF",
                optimistic=False,
                retain=False,
            )
        # Every scalar returned by the inverter's model endpoints remains
        # available to Home Assistant without making the default device noisy.
        # Normalized entities above are the stable public interface.
        components.update(
            self._raw_components(child, topic, pv, measurement_availability, serial)
        )
        components.update(self._diagnostic_components(child, topic, pv, measurement_availability))
        self._status_attributes(components, topic, pv)
        self._publish_config(child, self._config(self._device(child, f"PV Link {serial}", "PV Link", root), components), force)

    def _raw_components(self, root, topic, state, availability, serial=None):
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
                    self._model_availability(availability, serial, model)
                    if serial
                    else availability,
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
