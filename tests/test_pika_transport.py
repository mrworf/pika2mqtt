import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from pika_transport import (
    HostKeyMismatch,
    HostKeyUnavailable,
    HostKeyVerifier,
    SshTunnelConfig,
    SshTunnelSupervisor,
    TransportConfigurationError,
)
from pika2mqtt import Pika, PikaDevice, PikaMonitor


EXPECTED_FINGERPRINT = "SHA256:eNkV/mgYpbPMP9aSi/KM+9z9GwWmROMUrtmVr7TezY4"


class HostKeyVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.key = Path(self.temporary.name, "id_rsa")
        self.key.write_text("test key", encoding="utf-8")
        self.config = SshTunnelConfig(
            hostname="inverter.local",
            private_key=str(self.key),
            host_fingerprint=EXPECTED_FINGERPRINT,
        )

    def tearDown(self):
        self.temporary.cleanup()

    @mock.patch("pika_transport.subprocess.run")
    def test_writes_only_matching_key_to_known_hosts(self, run):
        key_line = "inverter.local ssh-ed25519 AAAATEST"
        run.side_effect = [
            subprocess.CompletedProcess([], 0, key_line + "\n", ""),
            subprocess.CompletedProcess(
                [], 0, f"256 {EXPECTED_FINGERPRINT} inverter.local (ED25519)\n", ""
            ),
        ]

        path = HostKeyVerifier(self.config, self.temporary.name).verify()

        self.assertEqual(Path(path).read_text(encoding="utf-8"), key_line + "\n")

    @mock.patch("pika_transport.subprocess.run")
    def test_rejects_changed_host_key(self, run):
        run.side_effect = [
            subprocess.CompletedProcess(
                [], 0, "inverter.local ssh-ed25519 AAAAOTHER\n", ""
            ),
            subprocess.CompletedProcess(
                [], 0, "256 SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA inverter.local (ED25519)\n", ""
            ),
        ]

        with self.assertRaises(HostKeyMismatch):
            HostKeyVerifier(self.config, self.temporary.name).verify()

    @mock.patch("pika_transport.subprocess.run")
    def test_scan_failure_is_transient(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "offline")
        with self.assertRaises(HostKeyUnavailable):
            HostKeyVerifier(self.config, self.temporary.name).verify()


class TunnelConfigurationTests(unittest.TestCase):
    @mock.patch("pika_transport.shutil.which", return_value="/usr/bin/tool")
    def test_valid_config_and_strict_tunnel_command(self, _which):
        with tempfile.NamedTemporaryFile() as key:
            config = SshTunnelConfig(
                hostname="inverter.local",
                private_key=key.name,
                host_fingerprint=EXPECTED_FINGERPRINT,
            )
            config.validate()
            command = SshTunnelSupervisor(config)._build_command("/tmp/known_hosts")

        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("127.0.0.1:18080:127.0.0.1:80", command)
        self.assertNotIn("StrictHostKeyChecking=no", command)

    @mock.patch("pika_transport.shutil.which", return_value="/usr/bin/tool")
    def test_missing_key_and_malformed_fingerprint_are_rejected(self, _which):
        with self.assertRaises(TransportConfigurationError):
            SshTunnelConfig("host", "/missing", EXPECTED_FINGERPRINT).validate()
        with tempfile.NamedTemporaryFile() as key:
            with self.assertRaises(TransportConfigurationError):
                SshTunnelConfig("host", key.name, "not-a-fingerprint").validate()

    def test_backoff_grows_and_is_capped(self):
        config = SshTunnelConfig("host", "/key", EXPECTED_FINGERPRINT)
        tunnel = SshTunnelSupervisor(config)
        with mock.patch("pika_transport.random.uniform", side_effect=lambda low, high: 1.0):
            self.assertEqual([tunnel._backoff_delay(i) for i in range(1, 8)], [1, 2, 4, 8, 16, 32, 60])

    def test_three_transport_failures_recycle_tunnel(self):
        config = SshTunnelConfig("host", "/key", EXPECTED_FINGERPRINT)
        tunnel = SshTunnelSupervisor(config)
        process = mock.Mock()
        process.poll.return_value = None
        tunnel._process = process
        tunnel._available.set()

        tunnel.report_transport_failure(OSError("one"))
        tunnel.report_transport_failure(OSError("two"))
        self.assertTrue(tunnel.is_available())
        tunnel.report_transport_failure(OSError("three"))

        self.assertFalse(tunnel.is_available())
        process.terminate.assert_called_once()

    @mock.patch("pika_transport.socket.create_connection")
    @mock.patch("pika_transport.subprocess.Popen")
    @mock.patch("pika_transport.HostKeyVerifier.verify", return_value="/tmp/known_hosts")
    @mock.patch("pika_transport.SshTunnelConfig.validate")
    def test_supervisor_establishes_and_stops_one_tunnel(
        self, _validate, _verify, popen, create_connection
    ):
        class FakeProcess:
            def __init__(self):
                self.returncode = None
                self.stderr = []
                self.terminate_calls = 0

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminate_calls += 1
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        process = FakeProcess()
        popen.return_value = process
        create_connection.return_value = mock.MagicMock()
        config = SshTunnelConfig("host", "/key", EXPECTED_FINGERPRINT)
        tunnel = SshTunnelSupervisor(config)

        with self.assertLogs("pika_transport", level="INFO") as logs:
            tunnel.start()
            self.assertTrue(tunnel.wait_available(2))
            tunnel.stop()

        popen.assert_called_once()
        self.assertEqual(process.terminate_calls, 1)
        self.assertIn("SSH tunnel established", "\n".join(logs.output))
        self.assertEqual(tunnel.state, "stopping")


class CollectorTransportTests(unittest.TestCase):
    def fixture(self, name):
        return json.loads(Path("tests/fixtures", name).read_text(encoding="utf-8"))

    def test_live_shape_fixture_is_parsed_without_mqtt_changes(self):
        pika = Pika()
        pika.update(self.fixture("devices.json"))

        self.assertEqual(pika.find(type=PikaDevice.INVERTER).modid, 9)
        self.assertEqual(pika.find(type=PikaDevice.BATTERY).charge, 29.9)
        self.assertEqual(pika.find(type=PikaDevice.SOLAR).output, 312)

    @mock.patch("pika2mqtt.requests.get")
    def test_devices_are_loaded_through_loopback_tunnel(self, get):
        response = mock.Mock(status_code=200)
        response.json.return_value = self.fixture("devices.json")
        get.return_value = response
        transport = mock.Mock()
        monitor = PikaMonitor(
            "http://127.0.0.1:18080", "house/energy", transport=transport
        )

        pika = monitor.load_devices()

        self.assertIsNotNone(pika)
        get.assert_called_once_with("http://127.0.0.1:18080/devices", timeout=5)
        transport.report_success.assert_called_once()

    @mock.patch("pika2mqtt.requests.get")
    def test_request_failure_is_reported_to_supervisor(self, get):
        error = requests.exceptions.Timeout("hung")
        get.side_effect = error
        transport = mock.Mock()
        monitor = PikaMonitor(
            "http://127.0.0.1:18080", "house/energy", transport=transport
        )

        self.assertIsNone(monitor.load_devices())
        transport.report_transport_failure.assert_called_once_with(error)


if __name__ == "__main__":
    unittest.main()
