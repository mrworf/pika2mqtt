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
        "system": {"solar_power_w": 1200, "learned_string_count": 1, "connected_string_count": 1, "disconnected_string_count": 0, "faulted_string_count": 0, "any_string_disconnected": False, "any_string_faulted": False},
        "inverter": {"serial": "0001000706FA", "power_w": 1000, "accumulated_energy_kwh": 42, "status": "making_power"},
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
        child = decoded["homeassistant/device/pika2mqtt_pv_00010003119c/config"]
        self.assertEqual(child["device"]["via_device"], "pika2mqtt_0001000706fa")
        self.assertIn("disconnected", child["components"])
        self.assertIn("fault", child["components"])

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


if __name__ == "__main__":
    unittest.main()
