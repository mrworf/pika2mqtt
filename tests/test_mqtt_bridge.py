import copy
import json
import types
import unittest
from unittest import mock

from jinja2 import Environment, StrictUndefined

from mqtt_bridge import MqttBridge


def validate_discovery_components(config):
    """Enforce HA's device-component platform/removal contract, not its full schema."""
    for key, component in config["components"].items():
        if component.get("platform") not in ("sensor", "binary_sensor", "switch", "select"):
            raise ValueError(f"Missing or unsupported discovery platform: {key}")
        if len(component) > 1 and "unique_id" not in component:
            raise ValueError(f"Missing discovery unique_id: {key}")


class PublishResult:
    def __init__(self, rc=0):
        self.rc = rc
        self.waited = False

    def wait_for_publish(self, timeout=None):
        self.waited = True


class FakeClient:
    def __init__(self):
        self.published = []
        self.subscribed = []
        self.disconnected = False

    def will_set(self, *args, **kwargs):
        self.will = (args, kwargs)

    def reconnect_delay_set(self, **kwargs):
        self.reconnect_delay = kwargs

    def publish(self, topic, payload, **kwargs):
        if "/device/" in topic and topic.endswith("/config") and payload:
            validate_discovery_components(json.loads(payload))
        result = PublishResult()
        self.published.append((topic, payload, kwargs, result))
        return result

    def subscribe(self, topic, qos):
        self.subscribed.append((topic, qos))

    def disconnect(self):
        self.disconnected = True


def snapshot():
    return {
        "api_connected": True,
        "system": {"solar_power_w": 1200, "learned_string_count": 1, "connected_string_count": 1, "disconnected_string_count": 0, "faulted_string_count": 0, "unknown_fault_string_count": 0, "any_string_disconnected": False, "any_string_faulted": False, "untracked_pv_link_count": 0, "untracked_pv_links": []},
        "inverter": {"serial": "0001000706FA", "power_w": 1000, "accumulated_energy_kwh": 42, "status": "making_power", "system_operating_mode": "Clean Backup"},
        "grid": {"power_w": 500, "import_power_w": 0, "export_power_w": 500, "import_energy_kwh": 2, "export_energy_kwh": 41},
        "batteries": [{"serial": "000100080701", "power_w": -100, "input_power_w": 100, "output_power_w": 0, "state_of_charge_percent": 90.5, "status": "charging_battery"}],
        "pv_links": {"00010003119C": {"serial": "00010003119C", "connected": True, "fault": False, "power_w": 1200, "status": "making_power", "last_heard_seconds": 2, "enabled": True, "enable_control_available": True, "core_state_available": True, "detailed_fault_coverage": True}},
    }


def snapshot_with_battery_modules():
    data = snapshot()
    data["battery_modules"] = {
        "000100080701": {
            "battery_serial": "000100080701",
            "endpoint_health": {"fresh": True},
            "modules": {
                "1": {
                    "index": 1,
                    "present": True,
                    "state_of_charge_percent": 68,
                    "state_of_health_percent": 99.5,
                    "cell_count": 13,
                    "minimum_cell_voltage_v": 3.89,
                    "maximum_cell_voltage_v": 3.91,
                    "average_cell_voltage_v": 3.9,
                    "minimum_cell_temperature_c": 25.3,
                    "maximum_cell_temperature_c": 29.4,
                    "average_cell_temperature_c": 27.5,
                },
                "2": {"index": 2, "present": False},
            },
        }
    }
    return data


class MqttBridgeTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.bridge = MqttBridge(self.client, "house/energy/", "inverter.local")

    def connect(self):
        self.bridge.on_connect(self.client, None, None, 0)

    def configs(self):
        return {topic: json.loads(payload) for topic, payload, _, _ in self.client.published
                if topic.startswith("homeassistant/device/")}

    def test_device_discovery_accepts_typed_removals_and_rejects_empty_components(self):
        for platform in ("binary_sensor", "sensor"):
            validate_discovery_components({"components": {"old": {"platform": platform}}})
        for component in ({}, {"unique_id": "old"}, {"platform": "sensor", "name": "Invalid"}):
            with self.subTest(component=component), self.assertRaises(ValueError):
                validate_discovery_components({"components": {"old": component}})

    def test_truth_states_polarity_and_missing_values(self):
        data = snapshot()
        data["system"]["register_definitions"] = {"available": True}
        self.bridge.publish_snapshot(data)
        self.connect()
        configs = self.configs()
        parent = configs["homeassistant/device/pika2mqtt_0001000706fa/config"]["components"]
        pv = configs["homeassistant/device/pika2mqtt_pv_00010003119c/config"]["components"]
        cases = [
            (pv["disconnected_truth"], "connected", True),
            (pv["fault_truth"], "fault", False),
            (pv["enabled_truth"], "enabled", False),
            (pv["detailed_fault_data_unavailable_truth"], "detailed_fault_coverage", True),
            (parent["any_string_disconnected_truth"], "any_string_disconnected", False),
            (parent["any_string_faulted_truth"], "any_string_faulted", False),
            (parent["decoded_definitions_available_truth"], "register_definitions.available", False),
        ]
        environment = Environment(undefined=StrictUndefined)
        for component, key, inverted in cases:
            with self.subTest(name=component["name"]):
                self.assertEqual(component["platform"], "sensor")
                self.assertTrue(component["unique_id"].endswith("_truth"))
                for removed in ("payload_on", "payload_off", "device_class", "state_class", "unit_of_measurement"):
                    self.assertNotIn(removed, component)
                template = environment.from_string(component["value_template"])
                for value in (True, False, None, 0, 1, "False"):
                    state = {key: value} if "." not in key else {"register_definitions": {"available": value}}
                    expected = str(not value if inverted else value) if isinstance(value, bool) else "unknown"
                    self.assertEqual(template.render(value_json=state), expected)
                self.assertEqual(template.render(value_json={}), "unknown")
                if "." in key:
                    for state in ({"register_definitions": None}, {"register_definitions": {}}):
                        self.assertEqual(template.render(value_json=state), "unknown")
        # Discovery presentation does not turn the underlying JSON into strings.
        state = json.loads(next(p for t, p, _, _ in self.client.published if t.endswith("state/pv/00010003119C")))
        self.assertIs(state["enabled"], True)
        self.assertIs(state["fault"], False)

    def test_truth_cleanup_does_not_remove_replacements_on_birth_or_restart(self):
        self.bridge.publish_snapshot(snapshot())
        self.connect()
        topic = "homeassistant/device/pika2mqtt_pv_00010003119c/config"
        configs = [json.loads(p) for t, p, _, _ in self.client.published if t == topic]
        self.assertEqual(configs[0]["components"]["disconnected"], {"platform": "binary_sensor"})
        self.assertNotIn("disconnected_truth", configs[0]["components"])
        self.assertIn("power", configs[0]["components"])
        final = configs[-1]
        for name in ("disconnected", "fault", "enabled", "detailed_fault_data_unavailable"):
            self.assertEqual(final["components"][name], {"platform": "binary_sensor"})
            self.assertIn(name + "_truth", final["components"])
        self.bridge.republish(force_discovery=True)
        self.bridge = MqttBridge(self.client, "house/energy", "inverter.local")
        self.bridge.publish_snapshot(snapshot())
        self.connect()
        for t, payload, _, _ in self.client.published:
            if t.startswith("homeassistant/device/"):
                components = json.loads(payload)["components"]
                self.assertFalse(any(c.get("platform") == "binary_sensor" and len(c) > 1 for c in components.values()))
                self.assertFalse(any(k.endswith("_truth") and len(c) == 1 for k, c in components.items()))

    def test_failed_truth_cleanup_is_retried_before_replacements(self):
        config = self.bridge._config({}, {"flag": self.bridge._binary("root", "flag", "Flag", "state", availability=[])})
        with mock.patch.object(self.bridge, "_publish", return_value=PublishResult(1)) as publish:
            self.bridge._publish_config("root", config, False)
            self.assertEqual(publish.call_count, 1)
            self.assertNotIn("flag_truth", json.loads(publish.call_args.args[1])["components"])
        self.assertEqual(self.bridge._discovery_payloads, {})
        with mock.patch.object(self.bridge, "_publish", return_value=PublishResult()) as publish:
            self.bridge._publish_config("root", config, False)
            self.assertEqual(publish.call_count, 2)
            self.assertIn("flag_truth", json.loads(publish.call_args.args[1])["components"])

    def test_null_fault_and_enabled_keep_existing_unavailable_topics(self):
        data = snapshot()
        data["pv_links"]["00010003119C"]["enabled"] = None
        data["pv_links"]["00010003119C"]["fault"] = None
        self.bridge.publish_snapshot(data)
        self.connect()
        values = {(topic, payload) for topic, payload, _, _ in self.client.published}
        for name in ("enabled", "fault"):
            self.assertIn(("house/energy/availability/pv/00010003119C/" + name, "unavailable"), values)

    def test_configures_disconnected_lwt_and_bounded_reconnect(self):
        self.assertEqual(self.client.will, (("house/energy/availability/service", "disconnected"), {"qos": 1, "retain": True}))
        self.assertEqual(self.client.reconnect_delay, {"min_delay": 1, "max_delay": 60})

    def test_gates_state_until_connack_then_publishes_retained_qos_one(self):
        self.bridge.publish_snapshot(snapshot())
        self.assertEqual(self.client.published, [])
        self.connect()
        state = [entry for entry in self.client.published if entry[0] == "house/energy/state/system"]
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0][2], {"qos": 1, "retain": True})
        self.assertEqual(json.loads(state[0][1])["solar_power_w"], 1200)
        self.assertIn(("homeassistant/status", 1), self.client.subscribed)

    def test_invalid_power_is_omitted_and_only_power_availability_changes(self):
        data = snapshot()
        data["system"].pop("solar_power_w")
        data["pv_links"]["00010003119C"].pop("power_w")
        self.bridge.publish_snapshot(data)
        self.connect()

        values = {(topic, payload) for topic, payload, _, _ in self.client.published}
        self.assertIn(("house/energy/availability/power/solar", "unavailable"), values)
        self.assertIn(
            ("house/energy/availability/power/pv/00010003119C", "unavailable"),
            values,
        )
        self.assertIn(("house/energy/availability/pv/00010003119C", "connected"), values)

        system_payload = next(
            payload for topic, payload, _, _ in self.client.published
            if topic == "house/energy/state/system"
        )
        pv_payload = next(
            payload for topic, payload, _, _ in self.client.published
            if topic == "house/energy/state/pv/00010003119C"
        )
        self.assertNotIn("solar_power_w", json.loads(system_payload))
        self.assertNotIn("power_w", json.loads(pv_payload))
        self.assertEqual(json.loads(pv_payload)["status"], "making_power")

        configs = {
            topic: json.loads(payload)
            for topic, payload, _, _ in self.client.published
            if topic.startswith("homeassistant/device/")
        }
        parent = configs["homeassistant/device/pika2mqtt_0001000706fa/config"]
        pv = configs["homeassistant/device/pika2mqtt_pv_00010003119c/config"]
        self.assertEqual(
            parent["components"]["solar_power"]["availability"][-1],
            {
                "topic": "house/energy/availability/power/solar",
                "payload_available": "available",
                "payload_not_available": "unavailable",
            },
        )
        self.assertEqual(
            pv["components"]["power"]["availability"][-1]["topic"],
            "house/energy/availability/power/pv/00010003119C",
        )

    def test_valid_power_publishes_available_quality_topics(self):
        self.bridge.publish_snapshot(snapshot())
        self.connect()
        values = {(topic, payload) for topic, payload, _, _ in self.client.published}
        self.assertIn(("house/energy/availability/power/solar", "available"), values)
        self.assertIn(
            ("house/energy/availability/power/pv/00010003119C", "available"),
            values,
        )

    def test_discovery_has_parent_battery_and_pv_child_with_separate_health(self):
        self.bridge.publish_snapshot(snapshot())
        self.connect()
        configs = [entry for entry in self.client.published if entry[0].startswith("homeassistant/device/")]
        decoded = {entry[0]: json.loads(entry[1]) for entry in configs}
        parent_topic = "homeassistant/device/pika2mqtt_0001000706fa/config"
        self.assertIn(parent_topic, decoded)
        parent = decoded[parent_topic]
        self.assertEqual(len(decoded), 3)
        self.assertIn("any_string_disconnected_truth", parent["components"])
        self.assertIn("any_string_faulted_truth", parent["components"])
        mode = parent["components"]["system_operating_mode"]
        self.assertEqual(mode["name"], "System Operating Mode")
        self.assertEqual(mode["value_template"], "{{ value_json.system_operating_mode }}")
        child = decoded["homeassistant/device/pika2mqtt_pv_00010003119c/config"]
        self.assertEqual(child["device"]["via_device"], "pika2mqtt_0001000706fa")
        self.assertIn("disconnected_truth", child["components"])
        self.assertIn("fault_truth", child["components"])

    def test_battery_modules_publish_child_state_availability_and_discovery(self):
        self.bridge.publish_snapshot(snapshot_with_battery_modules())
        self.connect()

        values = {(topic, payload) for topic, payload, _, _ in self.client.published}
        self.assertIn(
            (
                "house/energy/availability/battery/000100080701/modules",
                "available",
            ),
            values,
        )
        self.assertIn(
            (
                "house/energy/availability/battery/000100080701/module/1",
                "available",
            ),
            values,
        )
        self.assertIn(
            (
                "house/energy/availability/battery/000100080701/module/2",
                "unavailable",
            ),
            values,
        )
        module_state = next(
            json.loads(payload)
            for topic, payload, _, _ in self.client.published
            if topic == "house/energy/state/battery/000100080701/module/1"
        )
        self.assertEqual(module_state["state_of_charge_percent"], 68)

        config = next(
            json.loads(payload)
            for topic, payload, _, _ in self.client.published
            if topic
            == "homeassistant/device/pika2mqtt_battery_000100080701_module_1/config"
        )
        self.assertEqual(
            config["device"]["via_device"],
            "pika2mqtt_battery_000100080701",
        )
        self.assertEqual(config["device"]["name"], "PWRcell Battery Module 1")
        soc = config["components"]["state_of_charge_percent"]
        self.assertNotIn("enabled_by_default", soc)
        self.assertEqual(soc["device_class"], "battery")
        self.assertEqual(
            {entry["topic"] for entry in soc["availability"]},
            {
                "house/energy/availability/service",
                "house/energy/availability/inverter",
                "house/energy/availability/battery/000100080701/modules",
                "house/energy/availability/battery/000100080701/module/1",
            },
        )
        diagnostic = config["components"]["average_cell_voltage_v"]
        self.assertFalse(diagnostic["enabled_by_default"])
        self.assertEqual(diagnostic["entity_category"], "diagnostic")

        missing_config = next(
            json.loads(payload)
            for topic, payload, _, _ in self.client.published
            if topic
            == "homeassistant/device/pika2mqtt_battery_000100080701_module_2/config"
        )
        self.assertEqual(
            missing_config["device"]["via_device"],
            "pika2mqtt_battery_000100080701",
        )

    def test_stale_battery_module_model_unavailable_without_stale_state(self):
        data = snapshot_with_battery_modules()
        self.bridge.publish_snapshot(data)
        self.connect()
        self.client.published.clear()

        stale = copy.deepcopy(data)
        stale["battery_modules"]["000100080701"]["endpoint_health"]["fresh"] = False
        stale["battery_modules"]["000100080701"]["modules"] = {}
        self.bridge.publish_snapshot(stale)

        values = {(topic, payload) for topic, payload, _, _ in self.client.published}
        self.assertIn(
            (
                "house/energy/availability/battery/000100080701/modules",
                "unavailable",
            ),
            values,
        )
        self.assertIn(
            (
                "house/energy/availability/battery/000100080701/module/1",
                "unavailable",
            ),
            values,
        )
        self.assertFalse(
            any(
                topic == "house/energy/state/battery/000100080701/module/1"
                for topic, _, _, _ in self.client.published
            )
        )

    def test_battery_module_data_does_not_change_aggregate_battery_contract(self):
        def aggregate_publications(data):
            client = FakeClient()
            bridge = MqttBridge(client, "house/energy", "inverter.local")
            bridge.publish_snapshot(data)
            bridge.on_connect(client, None, None, 0)
            state = next(
                payload
                for topic, payload, _, _ in client.published
                if topic == "house/energy/state/battery/000100080701"
            )
            config = next(
                payload
                for topic, payload, _, _ in client.published
                if topic
                == "homeassistant/device/pika2mqtt_battery_000100080701/config"
            )
            return state, config

        self.assertEqual(
            aggregate_publications(snapshot()),
            aggregate_publications(snapshot_with_battery_modules()),
        )

    def test_operating_mode_control_is_absent_and_unsubscribed_by_default(self):
        self.bridge.publish_snapshot(snapshot())
        self.connect()
        parent = next(
            json.loads(payload)
            for topic, payload, _, _ in self.client.published
            if topic == "homeassistant/device/pika2mqtt_0001000706fa/config"
        )
        self.assertNotIn("system_operating_mode_control", parent["components"])
        self.assertNotIn(
            ("house/energy/command/system_operating_mode", 1),
            self.client.subscribed,
        )
        self.assertNotIn(
            ("house/energy/command/pv/+/enabled", 1),
            self.client.subscribed,
        )

    def test_enabled_operating_mode_control_discovers_select_and_subscribes(self):
        bridge = MqttBridge(
            self.client,
            "house/energy",
            "inverter.local",
            operating_mode_control_enabled=True,
        )
        bridge.publish_snapshot(snapshot())
        bridge.on_connect(self.client, None, None, 0)
        self.assertIn(
            ("house/energy/command/system_operating_mode", 1),
            self.client.subscribed,
        )
        self.assertIn(
            ("house/energy/command/pv/+/enabled", 1),
            self.client.subscribed,
        )
        parent = next(
            json.loads(payload)
            for topic, payload, _, _ in self.client.published
            if topic == "homeassistant/device/pika2mqtt_0001000706fa/config"
        )
        self.assertIn("system_operating_mode", parent["components"])
        control = parent["components"]["system_operating_mode_control"]
        self.assertEqual(control["platform"], "select")
        self.assertEqual(
            control["command_topic"],
            "house/energy/command/system_operating_mode",
        )
        self.assertEqual(
            control["options"],
            ["Grid Tie", "Self Supply", "Clean Backup", "Priority Backup"],
        )
        self.assertFalse(control["optimistic"])
        self.assertFalse(control["retain"])
        self.assertEqual(control["value_template"], "{{ value_json.system_operating_mode }}")
        child = next(
            json.loads(payload)
            for topic, payload, _, _ in self.client.published
            if topic == "homeassistant/device/pika2mqtt_pv_00010003119c/config"
        )
        pv_control = child["components"]["enabled_control"]
        self.assertEqual(pv_control["platform"], "switch")
        self.assertEqual(pv_control["name"], "Enabled Control")
        self.assertEqual(
            pv_control["command_topic"],
            "house/energy/command/pv/00010003119C/enabled",
        )
        self.assertEqual(pv_control["payload_on"], "ON")
        self.assertEqual(pv_control["payload_off"], "OFF")
        self.assertFalse(pv_control["optimistic"])
        self.assertFalse(pv_control["retain"])
        self.assertEqual(
            pv_control["availability"][-1]["topic"],
            "house/energy/availability/control/pv/00010003119C",
        )

    def test_pv_link_commands_validate_and_dispatch_exact_payloads(self):
        calls = []
        bridge = MqttBridge(
            self.client,
            "house/energy",
            "inverter.local",
            operating_mode_control_enabled=True,
        )
        bridge.set_pv_link_command_handler(
            lambda serial, enabled: calls.append((serial, enabled))
        )
        topic = "house/energy/command/pv/00010003119c/enabled"
        for payload, enabled in ((b"ON", True), (b"OFF", False)):
            bridge.on_message(
                self.client,
                None,
                types.SimpleNamespace(topic=topic, payload=payload, retain=False),
            )
            self.assertEqual(calls[-1], ("00010003119C", enabled))

        count = len(calls)
        with self.assertLogs("mqtt_bridge", level="WARNING"):
            for payload, retained in ((b"on", False), (b" ON", False), (b"ON", True)):
                bridge.on_message(
                    self.client,
                    None,
                    types.SimpleNamespace(
                        topic=topic,
                        payload=payload,
                        retain=retained,
                    ),
                )
        self.assertEqual(len(calls), count)

    def test_disabled_pv_link_control_ignores_direct_command(self):
        calls = []
        self.bridge.set_pv_link_command_handler(lambda *args: calls.append(args))
        with self.assertLogs("mqtt_bridge", level="WARNING"):
            self.bridge.on_message(
                self.client,
                None,
                types.SimpleNamespace(
                    topic="house/energy/command/pv/00010003119C/enabled",
                    payload=b"OFF",
                    retain=False,
                ),
            )
        self.assertEqual(calls, [])

    def test_pv_control_and_enabled_availability_publish_independently(self):
        bridge = MqttBridge(
            self.client,
            "house/energy",
            "inverter.local",
            operating_mode_control_enabled=True,
        )
        data = snapshot()
        data["pv_links"]["00010003119C"]["enable_control_available"] = False
        bridge.publish_snapshot(data)
        bridge.on_connect(self.client, None, None, 0)
        values = {(topic, payload) for topic, payload, _, _ in self.client.published}
        self.assertIn(
            ("house/energy/availability/pv/00010003119C/enabled", "available"),
            values,
        )
        self.assertIn(
            ("house/energy/availability/control/pv/00010003119C", "unavailable"),
            values,
        )

    def test_operating_mode_commands_validate_and_dispatch_exact_options(self):
        calls = []
        bridge = MqttBridge(
            self.client,
            "house/energy",
            "inverter.local",
            operating_mode_control_enabled=True,
        )
        bridge.set_operating_mode_command_handler(
            lambda label, code: calls.append((label, code))
        )
        topic = "house/energy/command/system_operating_mode"
        for label, code in (
            ("Grid Tie", 1),
            ("Self Supply", 2),
            ("Clean Backup", 3),
            ("Priority Backup", 4),
        ):
            bridge.on_message(
                self.client,
                None,
                types.SimpleNamespace(topic=topic, payload=label.encode(), retain=False),
            )
            self.assertEqual(calls[-1], (label, code))

        count = len(calls)
        rejected = (
            types.SimpleNamespace(topic=topic, payload=b"Sell", retain=False),
            types.SimpleNamespace(topic=topic, payload=b" Self Supply", retain=False),
            types.SimpleNamespace(topic=topic, payload=b"\xff", retain=False),
            types.SimpleNamespace(topic=topic, payload=b"Grid Tie", retain=True),
        )
        with self.assertLogs("mqtt_bridge", level="WARNING"):
            for message in rejected:
                bridge.on_message(self.client, None, message)
        self.assertEqual(len(calls), count)

    def test_disabled_operating_mode_control_ignores_direct_command(self):
        calls = []
        self.bridge.set_operating_mode_command_handler(lambda *args: calls.append(args))
        with self.assertLogs("mqtt_bridge", level="WARNING"):
            self.bridge.on_message(
                self.client,
                None,
                types.SimpleNamespace(
                    topic="house/energy/command/system_operating_mode",
                    payload=b"Self Supply",
                    retain=False,
                ),
            )
        self.assertEqual(calls, [])

    def test_inverter_and_string_availability_are_distinct(self):
        data = snapshot()
        data["api_connected"] = False
        data["pv_links"]["00010003119C"]["connected"] = False
        self.bridge.publish_snapshot(data)
        self.connect()
        values = {(topic, payload) for topic, payload, _, _ in self.client.published}
        self.assertIn(("house/energy/availability/service", "connected"), values)
        self.assertIn(("house/energy/availability/inverter", "disconnected"), values)
        self.assertIn(("house/energy/availability/pv/00010003119C", "disconnected"), values)

    def test_model_freshness_controls_only_dependent_entities(self):
        data = snapshot()
        pv = data["pv_links"]["00010003119C"]
        pv["endpoint_health"] = {
            "common": {"fresh": True},
            "REbus_status": {"fresh": True},
            "pvlink_status": {"fresh": False},
            "pvrss_telemetry": {"fresh": True},
        }
        self.bridge.publish_snapshot(data)
        self.connect()

        values = {(topic, payload) for topic, payload, _, _ in self.client.published}
        self.assertIn(
            (
                "house/energy/availability/model/00010003119C/pvlink_status",
                "unavailable",
            ),
            values,
        )
        self.assertIn(
            (
                "house/energy/availability/model/00010003119C/REbus_status",
                "available",
            ),
            values,
        )
        self.assertIn(
            ("house/energy/availability/pv/00010003119C/core", "available"),
            values,
        )
        self.assertIn(
            ("house/energy/availability/pv/00010003119C/fault", "available"),
            values,
        )

        child = next(
            json.loads(payload)
            for topic, payload, _, _ in reversed(self.client.published)
            if topic == "homeassistant/device/pika2mqtt_pv_00010003119c/config"
        )
        components = child["components"]
        self.assertEqual(components["disconnected_truth"]["name"], "Communication lost")
        self.assertEqual(
            components["disconnected_truth"]["unique_id"],
            "pika2mqtt_pv_00010003119c_disconnected_truth",
        )
        enabled_topics = {item["topic"] for item in components["enabled_truth"]["availability"]}
        status_topics = {item["topic"] for item in components["status"]["availability"]}
        fault_topics = {item["topic"] for item in components["fault_truth"]["availability"]}
        self.assertIn(
            "house/energy/availability/pv/00010003119C/enabled",
            enabled_topics,
        )
        self.assertNotIn(
            "house/energy/availability/model/00010003119C/REbus_status",
            enabled_topics,
        )
        self.assertIn(
            "house/energy/availability/pv/00010003119C/core",
            status_topics,
        )
        self.assertIn(
            "house/energy/availability/pv/00010003119C/fault",
            fault_topics,
        )
        self.assertNotIn(
            "house/energy/availability/model/00010003119C/pvlink_status",
            fault_topics,
        )
        coverage = components["detailed_fault_data_unavailable_truth"]
        self.assertFalse(coverage["enabled_by_default"])
        self.assertNotIn("device_class", coverage)

    def test_home_assistant_birth_and_reconnect_republish(self):
        self.bridge.publish_snapshot(snapshot())
        self.connect()
        first_count = len(self.client.published)
        self.bridge.on_message(self.client, None, types.SimpleNamespace(topic="homeassistant/status", payload=b"online"))
        self.assertGreater(len(self.client.published), first_count)
        self.bridge.on_disconnect(self.client, None, None, 7)
        disconnected_count = len(self.client.published)
        self.bridge.publish_snapshot(snapshot())
        self.assertEqual(len(self.client.published), disconnected_count)
        self.connect()
        self.assertGreater(len(self.client.published), disconnected_count)

    def test_graceful_shutdown_publishes_disconnected(self):
        self.connect()
        self.bridge.publish_snapshot(snapshot())
        self.bridge.shutdown()
        topic, payload, options, result = self.client.published[-1]
        self.assertEqual((topic, payload), ("house/energy/availability/service", "disconnected"))
        self.assertTrue(result.waited)
        self.assertTrue(self.client.disconnected)

    def test_rejected_connection_does_not_publish(self):
        self.bridge.publish_snapshot(snapshot())
        self.bridge.on_connect(self.client, None, None, 5)
        self.assertFalse(self.bridge.connected.is_set())
        self.assertEqual(self.client.published, [])

    def test_initial_connection_failure_is_logged_and_remains_gated(self):
        with self.assertLogs("mqtt_bridge", level="WARNING") as logs:
            self.bridge.on_connect_fail(self.client, None)
        self.assertFalse(self.bridge.connected.is_set())
        self.assertIn("automatic retry remains active", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
