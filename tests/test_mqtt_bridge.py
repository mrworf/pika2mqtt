import json
import types
import unittest

from mqtt_bridge import MqttBridge


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
        "pv_links": {"00010003119C": {"serial": "00010003119C", "connected": True, "fault": False, "power_w": 1200, "status": "making_power", "last_heard_seconds": 2}},
    }


class MqttBridgeTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.bridge = MqttBridge(self.client, "house/energy/", "inverter.local")

    def connect(self):
        self.bridge.on_connect(self.client, None, None, 0)

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
        self.assertEqual(len(configs), 3)
        decoded = {entry[0]: json.loads(entry[1]) for entry in configs}
        parent_topic = "homeassistant/device/pika2mqtt_0001000706fa/config"
        self.assertIn(parent_topic, decoded)
        parent = decoded[parent_topic]
        self.assertIn("any_string_disconnected", parent["components"])
        self.assertIn("any_string_faulted", parent["components"])
        mode = parent["components"]["system_operating_mode"]
        self.assertEqual(mode["name"], "System Operating Mode")
        self.assertEqual(mode["value_template"], "{{ value_json.system_operating_mode }}")
        child = decoded["homeassistant/device/pika2mqtt_pv_00010003119c/config"]
        self.assertEqual(child["device"]["via_device"], "pika2mqtt_0001000706fa")
        self.assertIn("disconnected", child["components"])
        self.assertIn("fault", child["components"])

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
