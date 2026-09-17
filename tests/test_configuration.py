import os
import unittest
from unittest import mock

from pika2mqtt import CollectorThread, build_parser, validate_arguments
from telemetry import OperatingModeError


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
        self.assertFalse(args.operating_mode_control)

    @mock.patch.dict(
        os.environ,
        {
            "PV_INVENTORY_FREEZE": "true",
            "HA_DISCOVERY_ENABLED": "false",
            "MQTT_PORT": "2883",
            "OPERATING_MODE_CONTROL_ENABLED": "true",
        },
        clear=True,
    )
    def test_environment_configures_freeze_discovery_and_mqtt_port(self):
        args = self.parse()
        validate_arguments(args)
        self.assertTrue(args.pv_inventory_freeze)
        self.assertFalse(args.ha_discovery)
        self.assertEqual(args.mqtt_port, 2883)
        self.assertTrue(args.operating_mode_control)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_operating_mode_control_cli_overrides_default(self):
        self.assertTrue(self.parse("--operating-mode-control").operating_mode_control)
        self.assertFalse(self.parse("--no-operating-mode-control").operating_mode_control)

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


class CollectorCommandTests(unittest.TestCase):
    class Publisher:
        def __init__(self):
            self.snapshots = []

        def publish_snapshot(self, snapshot):
            self.snapshots.append(snapshot)

    class Transport:
        def __init__(self, available=True):
            self.available = available

        def wait_available(self, timeout):
            return self.available

    def test_pending_operating_mode_commands_coalesce_to_latest(self):
        telemetry = mock.Mock()
        telemetry.set_system_operating_mode.return_value = {"confirmed": True}
        publisher = self.Publisher()
        monitor = CollectorThread(telemetry, publisher, self.Transport())

        monitor.request_operating_mode("Grid Tie", 1)
        monitor.request_operating_mode("Priority Backup", 4)
        self.assertTrue(monitor._process_pending_operating_mode())
        telemetry.set_system_operating_mode.assert_called_once_with(4)
        self.assertEqual(publisher.snapshots, [{"confirmed": True}])

    def test_failed_or_unavailable_command_does_not_publish(self):
        telemetry = mock.Mock()
        publisher = self.Publisher()
        unavailable = CollectorThread(telemetry, publisher, self.Transport(False))
        unavailable.request_operating_mode("Self Supply", 2)
        with self.assertLogs(level="ERROR"):
            self.assertFalse(unavailable._process_pending_operating_mode())
        telemetry.set_system_operating_mode.assert_not_called()

        telemetry.set_system_operating_mode.side_effect = OperatingModeError(
            "mocked failure"
        )
        failed = CollectorThread(telemetry, publisher, self.Transport(True))
        failed.request_operating_mode("Self Supply", 2)
        with self.assertLogs(level="ERROR"):
            self.assertFalse(failed._process_pending_operating_mode())
        self.assertEqual(publisher.snapshots, [])


if __name__ == "__main__":
    unittest.main()
