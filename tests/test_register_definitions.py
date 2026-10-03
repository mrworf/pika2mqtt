import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from register_definitions import DefinitionSet, RegisterDefinitions, atomic_write, load_policy
from pika_transport import SshTunnelConfig, SshTunnelSupervisor
from mqtt_bridge import MqttBridge
from tests.test_mqtt_bridge import FakeClient, snapshot


FIXTURE = Path("tests/fixtures/registers.xml")


def definitions(directory="/tmp", content=None):
    result = RegisterDefinitions(None, data_directory=directory)
    result.install({"registers.xml": content or FIXTURE.read_bytes()})
    return result


def device(value=0, fresh=True):
    return {"serial": "PV001", "kind": "pv", "connected": True,
            "raw_models": {
                "REbus_status": {"fixed": {"St": 8208, "Ev": 33536, "RB": 3}},
                "pvlink_status": {"fixed": {"ErrorWord": value}},
                "pvrss_telemetry": {"fixed": {"Status": 0, "SelfTestResults": 0}}},
            "endpoint_health": {name: {"fresh": fresh} for name in ("REbus_status", "pvlink_status", "pvrss_telemetry")}}


def archive(content=None):
    buffer = io.BytesIO()
    data = content or FIXTURE.read_bytes()
    with tarfile.open(fileobj=buffer, mode="w") as handle:
        info = tarfile.TarInfo("smdx/registers.xml")
        info.size = len(data)
        handle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class DefinitionTests(unittest.TestCase):
    def test_sparse_bits_reserved_and_unknown(self):
        d = definitions()
        r = d.decode("pvlink_status", "fixed", "ErrorWord", (1 << 1) | (1 << 9) | 1)
        self.assertEqual(r["errors"], ["HW_ARC_FAULT", "FLASH_FAILURE"])
        self.assertNotIn("0", r["flags"])
        self.assertEqual(r["unknown_mask"], 0)
        r = d.decode("REbus_status", "fixed", "RB", 1 << 10)
        self.assertEqual(r["unknown_mask"], 1 << 10)
        self.assertEqual(r["errors"], [])

    def test_enum_values_are_firmware_owned_and_exact(self):
        data = FIXTURE.read_bytes().replace(b">33536<", b">12345<")
        d = definitions(content=data)
        self.assertEqual(d.decode("REbus_status", "fixed", "Ev", 12345)["symbol"], "SYSMODE_CHANGE")
        self.assertTrue(d.decode("REbus_status", "fixed", "Ev", 33536)["unknown_code"])
        self.assertTrue(d.decode("REbus_status", "fixed", "St", 0x201F)["unknown_code"])
        for raw in (True, -1, 1.5, float("nan"), None):
            self.assertFalse(d.decode("REbus_status", "fixed", "St", raw)["available"])

    def test_stale_decoding_does_not_clear_errors(self):
        d = definitions()
        state = device(2)
        d.enrich(state)
        self.assertEqual(state["active_error_count"], 1)
        state = device(0, fresh=False)
        d.enrich(state)
        self.assertIsNone(state["active_error_count"])
        self.assertFalse(state["decoded_registers"]["pvlink_status.fixed.ErrorWord"]["available"])

    def test_lockout_comes_from_status_bit(self):
        d = definitions()
        state = device()
        state["raw_models"]["pvrss_telemetry"]["fixed"]["Status"] = 2
        d.enrich(state)
        self.assertIn("pvrss_telemetry.Status:LOCKOUT_ERROR", state["active_errors"])

    def test_missing_definitions_and_unknown_errors_are_unavailable(self):
        d = RegisterDefinitions(None)
        state = device()
        d.enrich(state)
        self.assertIsNone(state["active_error_count"])
        d = definitions()
        state = device(1 << 17)
        d.enrich(state)
        self.assertIsNone(state["active_error_count"])

    def test_repeating_records_have_independent_identity(self):
        d = DefinitionSet({"a.xml": FIXTURE.read_bytes()}, load_policy())
        decoded = d.decode_payload("lithium_ion_string", {"repeating": {"1": {"Evt": 16}, "2": {"Evt": 0}}}, True)
        self.assertEqual(decoded["lithium_ion_string.repeating.1.Evt"]["active_symbols"], ["TEST_MODULE_FAULT"])
        self.assertEqual(decoded["lithium_ion_string.repeating.2.Evt"]["active_symbols"], [])

    def test_duplicates_conflicts_and_malformed_xml(self):
        content = FIXTURE.read_bytes()
        d = DefinitionSet({"a.xml": content, "b.xml": content}, load_policy())
        self.assertFalse(d.issues)
        d = DefinitionSet({"a.xml": content, "b.xml": content.replace(b">33536<", b">12345<")}, load_policy())
        self.assertFalse(d.decode("REbus_status", "fixed", "Ev", 12345)["available"])
        d = DefinitionSet({"a.xml": content.replace(b'<symbol id="FLASH_FAILURE">9</symbol>', b'<symbol id="FLASH_FAILURE">1</symbol>')}, load_policy())
        self.assertFalse(d.decode("pvlink_status", "fixed", "ErrorWord", 2)["available"])
        for content in (b"not xml", b"<!DOCTYPE root><root/>", b"<root/>"):
            with self.assertRaises(ValueError):
                DefinitionSet({"bad.xml": content}, load_policy())

    def test_policy_override_is_complete_and_symbol_based(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "policy.json")
            policy = load_policy()
            policy["warnings"]["REbus_status.RB"] = ["HEARTBEAT_GOOD"]
            atomic_write(path, json.dumps(policy))
            d = DefinitionSet({"a.xml": FIXTURE.read_bytes()}, load_policy(path))
            self.assertEqual(d.decode("REbus_status", "fixed", "RB", 8192)["warnings"], ["HEARTBEAT_GOOD"])
            atomic_write(path, '{"version": 1}')
            with self.assertRaises(ValueError):
                load_policy(path)
            with self.assertRaises(ValueError):
                load_policy(Path(directory, "missing"))

    def test_firmware_change_invalidates_until_reloaded(self):
        d = definitions()
        d.note_versions({"INV": "1.8.2"})
        self.assertTrue(d.health()["available"])
        d.note_versions({"INV": "1.9"})
        self.assertFalse(d.health()["available"])
        self.assertIsNone(d.reference())
        self.assertIsNone(d.decode("REbus_status", "fixed", "Ev", 33536))
        d.install({"a.xml": FIXTURE.read_bytes()})
        self.assertIn("INV: 1.9", d.reference())

    def test_reference_includes_definitions_policy_and_escapes_html(self):
        d = definitions(content=FIXTURE.read_bytes().replace(b"Current device state.", b"&lt;script&gt;bad&lt;/script&gt;"))
        reference = d.reference()
        self.assertIn("HW_ARC_FAULT | 1 | error", reference)
        self.assertIn("No description provided by firmware", reference)
        self.assertIn(d.health()["checksum"], reference)
        self.assertNotIn("<script>", d.reference(html_format=True))
        self.assertIn("&lt;script&gt;", d.reference(html_format=True))

    def test_logging_transitions_and_recovery_without_false_clearance(self):
        d = definitions()
        with self.assertLogs("register_definitions", "INFO") as logs:
            d.enrich(device(2))
        self.assertTrue(any("initial observation" in line for line in logs.output))
        with self.assertNoLogs("register_definitions", "INFO"):
            d.enrich(device(2))
        with self.assertLogs("register_definitions", "INFO") as logs:
            d.enrich(device(0))
        self.assertTrue(any("error cleared: HW_ARC_FAULT" in line for line in logs.output))
        with self.assertLogs("register_definitions", "WARNING") as logs:
            d.enrich(device(2))
        self.assertTrue(any("error activated: HW_ARC_FAULT" in line for line in logs.output))
        with self.assertNoLogs("register_definitions", "INFO"):
            d.enrich(device(0, fresh=False))
        with self.assertLogs("register_definitions", "INFO") as logs:
            d.enrich(device(0))
        self.assertTrue(any("recovered observation" in line for line in logs.output))
        self.assertFalse(any("error cleared" in line for line in logs.output))

    def test_unknown_indicator_warning_is_suppressed_until_it_changes(self):
        d = definitions()
        state = device()
        state["raw_models"]["REbus_status"]["fixed"]["RB"] = 1024
        with self.assertLogs("register_definitions", "WARNING") as logs:
            d.enrich(state)
        self.assertTrue(any("unknown indicator" in line for line in logs.output))
        state["raw_models"]["REbus_status"]["fixed"]["RB"] = 1025
        with self.assertLogs("register_definitions", "WARNING") as logs:
            d.enrich(state)
        self.assertFalse(any("unknown indicator" in line for line in logs.output))

    def test_loader_reloads_on_connection_generation_and_writes_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = mock.Mock(connection_generation=1)
            transport.is_available.return_value = True
            transport.read_definition_archive.return_value = archive()
            d = RegisterDefinitions(transport, data_directory=directory)
            with mock.patch.object(d._wake, "wait") as wait:
                calls = []
                def advance(_):
                    calls.append(1)
                    if len(calls) == 1:
                        transport.connection_generation = 2
                    else:
                        d._stop.set()
                wait.side_effect = advance
                d._run()
            self.assertEqual(transport.read_definition_archive.call_count, 2)
            self.assertEqual(Path(directory, "register_reference.md").read_text(), d.reference())

    def test_loader_failure_retries_without_discarding_valid_snapshot(self):
        transport = mock.Mock(connection_generation=1)
        transport.is_available.return_value = True
        transport.read_definition_archive.side_effect = [OSError("offline"), archive()]
        with tempfile.TemporaryDirectory() as directory:
            d = RegisterDefinitions(transport, data_directory=directory)
            with mock.patch("register_definitions.time.monotonic", side_effect=[0, 0, 100, 100]), mock.patch.object(d._wake, "wait") as wait:
                count = []
                def stop(_):
                    count.append(1)
                    if len(count) == 2:
                        d._stop.set()
                wait.side_effect = stop
                with self.assertLogs("register_definitions", "WARNING"):
                    d._run()
            self.assertTrue(d.health()["available"])
            self.assertEqual(transport.read_definition_archive.call_count, 2)

    def test_atomic_write_failure_preserves_existing_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "reference.md")
            atomic_write(path, "original")
            with mock.patch("register_definitions.os.replace", side_effect=OSError("read-only")):
                with self.assertRaises(OSError):
                    atomic_write(path, "replacement")
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_reference_write_failure_does_not_interrupt_decoding(self):
        transport = mock.Mock(connection_generation=1)
        transport.is_available.return_value = True
        transport.read_definition_archive.return_value = archive()
        d = RegisterDefinitions(transport)
        with mock.patch.object(d._wake, "wait", side_effect=lambda _: d._stop.set()), mock.patch("register_definitions.atomic_write", side_effect=OSError("read-only")):
            with self.assertLogs("register_definitions", "WARNING") as logs:
                d._run()
        self.assertTrue(d.health()["available"])
        self.assertTrue(any("Cannot write register reference" in line for line in logs.output))


class DiagnosticMqttTests(unittest.TestCase):
    def data(self):
        d = definitions()
        data = snapshot()
        state = device(2)
        d.enrich(state)
        data["pv_links"]["00010003119C"].update(state)
        data["pv_links"]["00010003119C"]["serial"] = "00010003119C"
        data["system"]["register_definitions"] = d.health()
        return data

    def test_discovery_attributes_flags_availability_and_stable_identity(self):
        client = FakeClient()
        bridge = MqttBridge(client, "energy", "inv")
        bridge.on_connect(client, None, None, 0)
        data = self.data()
        bridge.publish_snapshot(data)
        config = json.loads([p[1] for p in client.published if p[0].endswith("pika2mqtt_pv_00010003119c/config")][-1])
        components = config["components"]
        flag = components["decoded_pvlink_status_fixed_errorword_bit_1"]
        self.assertFalse(flag["enabled_by_default"])
        self.assertIn("HW_ARC_FAULT", flag["value_template"])
        self.assertEqual(flag["device_class"], "problem")
        self.assertNotIn("decoded_pvlink_status_fixed_errorword_bit_0", components)
        event = components["decoded_rebus_status_fixed_ev"]
        self.assertTrue(event["enabled_by_default"])
        self.assertIn("supported_states", event["json_attributes_template"])
        self.assertIn("decoded_active_error_count", components)
        client.published.clear()
        data["pv_links"]["00010003119C"]["decoded_registers"]["pvlink_status.fixed.ErrorWord"]["available"] = False
        bridge.publish_snapshot(data)
        self.assertTrue(any(p[0].endswith("pvlink_status_fixed_errorword") and p[1] == "unavailable" for p in client.published))

    def test_manifest_removes_obsolete_components_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = str(Path(directory, "manifest.json"))
            client = FakeClient()
            data = self.data()
            bridge = MqttBridge(client, "energy", "inv", diagnostic_manifest_file=manifest)
            bridge.on_connect(client, None, None, 0)
            bridge.publish_snapshot(data)
            bridge = MqttBridge(client, "energy", "inv", diagnostic_manifest_file=manifest)
            bridge.on_connect(client, None, None, 0)
            data["pv_links"]["00010003119C"]["decoded_registers"].pop("REbus_status.fixed.RB")
            data["system"]["register_definitions"]["checksum"] = "new-firmware-definitions"
            client.published.clear()
            bridge.publish_snapshot(data)
            configs = [json.loads(p[1]) for p in client.published if p[0].endswith("pika2mqtt_pv_00010003119c/config")]
            self.assertEqual(configs[0]["components"]["decoded_rebus_status_fixed_rb_bit_0"], {})
            self.assertNotIn("decoded_rebus_status_fixed_rb_bit_0", configs[-1]["components"])

    def test_unavailable_definitions_do_not_prune_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = str(Path(directory, "manifest.json"))
            client = FakeClient()
            bridge = MqttBridge(client, "energy", "inv", diagnostic_manifest_file=manifest)
            bridge.on_connect(client, None, None, 0)
            data = self.data()
            bridge.publish_snapshot(data)
            original = Path(manifest).read_text()
            data["system"]["register_definitions"]["available"] = False
            data["pv_links"]["00010003119C"]["decoded_registers"] = {}
            bridge.publish_snapshot(data)
            self.assertEqual(Path(manifest).read_text(), original)

    def test_missing_observation_keeps_flags_and_invalidates_retained_availability(self):
        client = FakeClient()
        bridge = MqttBridge(client, "energy", "inv")
        bridge.on_connect(client, None, None, 0)
        data = self.data()
        bridge.publish_snapshot(data)
        data["pv_links"]["00010003119C"]["decoded_registers"] = {}
        client.published.clear()
        bridge.publish_snapshot(data)
        bridge.republish(force_discovery=True)
        config = json.loads([p[1] for p in client.published if p[0].endswith("pika2mqtt_pv_00010003119c/config")][-1])
        self.assertIn("decoded_rebus_status_fixed_rb_bit_0", config["components"])
        self.assertTrue(any(p[0].endswith("rebus_status_fixed_rb") and p[1] == "unavailable" for p in client.published))


class DefinitionTransportTests(unittest.TestCase):
    def test_ssh_read_reuses_verified_host_key_and_handles_timeout(self):
        tunnel = SshTunnelSupervisor(SshTunnelConfig("inv", "/key", "SHA256:" + "A" * 43))
        with self.assertRaises(OSError):
            tunnel.read_definition_archive()
        tunnel._available.set()
        tunnel._known_hosts = "/verified/known_hosts"
        with mock.patch("pika_transport.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"archive", b"")) as run:
            self.assertEqual(tunnel.read_definition_archive(), b"archive")
            self.assertIn("UserKnownHostsFile=/verified/known_hosts", run.call_args.args[0])
            self.assertIn("StrictHostKeyChecking=yes", run.call_args.args[0])
        with mock.patch("pika_transport.subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 30)):
            with self.assertRaises(OSError):
                tunnel.read_definition_archive()
