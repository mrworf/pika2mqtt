import os
import unittest
from unittest import mock

from pika2mqtt import build_parser, validate_arguments


class ConfigurationTests(unittest.TestCase):
    def parse(self, *extra):
        parser = build_parser()
        return parser.parse_args(["inverter.local", "mqtt.local", "house/energy", *extra])

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_new_defaults_enable_discovery_and_persistent_learning(self):
        args = self.parse()
        validate_arguments(args)
        self.assertEqual(args.mqtt_port, 1883)
        self.assertTrue(args.ha_discovery)
        self.assertEqual(args.ha_discovery_prefix, "homeassistant")
        self.assertEqual(args.pv_inventory_file, "/data/pv_inventory.json")
        self.assertFalse(args.pv_inventory_freeze)
        self.assertEqual(args.disconnect_after, 120)

    @mock.patch.dict(os.environ, {"PV_INVENTORY_FREEZE": "true", "HA_DISCOVERY_ENABLED": "false", "MQTT_PORT": "2883"}, clear=True)
    def test_environment_configures_freeze_discovery_and_mqtt_port(self):
        args = self.parse()
        validate_arguments(args)
        self.assertTrue(args.pv_inventory_freeze)
        self.assertFalse(args.ha_discovery)
        self.assertEqual(args.mqtt_port, 2883)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_partial_mqtt_credentials_are_rejected(self):
        args = self.parse("--user", "pika")
        with self.assertRaisesRegex(ValueError, "configured together"):
            validate_arguments(args)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_invalid_ranges_and_empty_discovery_prefix_are_rejected(self):
        for extra in (("--mqtt-port", "0"), ("--disconnect-after", "0"), ("--refresh", "0")):
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):
                    validate_arguments(self.parse(*extra))
        with self.assertRaises(ValueError):
            validate_arguments(self.parse("--ha-discovery-prefix", "/"))

    @mock.patch.dict(os.environ, {"PV_INVENTORY_FREEZE": "sometimes"}, clear=True)
    def test_invalid_boolean_environment_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be true or false"):
            build_parser()


if __name__ == "__main__":
    unittest.main()
