import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from telemetry import (
    InstallerTelemetry,
    InventoryError,
    PvInventory,
    decode_rebus_state,
    decode_system_operating_mode,
)


class Response:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload


class InventoryTests(unittest.TestCase):
    def test_learning_is_additive_unlimited_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "inventory.json")
            inventory = PvInventory(str(path), clock=lambda: 100)
            serials = [f"PV{index:03d}" for index in range(20)]
            self.assertEqual(inventory.observe(serials), tuple(serials))
            self.assertEqual(PvInventory(str(path)).serials, tuple(sorted(serials)))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_frozen_inventory_does_not_learn(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = PvInventory(str(Path(directory, "inventory.json")), frozen=True)
            self.assertEqual(inventory.observe(["PV001"]), ())
            self.assertEqual(inventory.serials, ())

    def test_corrupt_inventory_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "inventory.json")
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(InventoryError):
                PvInventory(str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), "not json")


class TelemetryTests(unittest.TestCase):
    def fixture(self, name):
        return json.loads(Path("tests/fixtures", name).read_text(encoding="utf-8"))

    def collector(self, directory, routes, now=1000, **kwargs):
        def get(url, timeout):
            path = url.removeprefix("http://installer")
            value = routes.get(path)
            if isinstance(value, Exception):
                raise value
            if value is None:
                return Response(status=500)
            return Response(value)

        inventory = PvInventory(str(Path(directory, "inventory.json")), clock=lambda: now)
        return InstallerTelemetry(
            "http://installer",
            inventory,
            clock=lambda: now,
            request_get=get,
            **kwargs,
        )

    def test_poll_enriches_all_device_types_and_native_grid_energy(self):
        with tempfile.TemporaryDirectory() as directory:
            devices = self.fixture("devices.json")
            routes = {"/devices": devices}
            for serial, mod_id, kind in [
                ("000100030001", 3, "pv"),
                ("000100070001", 9, "inverter"),
                ("000100080001", 10, "battery"),
            ]:
                routes[f"/device/{mod_id}/model/common"] = {"fixed": {"SN": serial}}
                routes[f"/device/{mod_id}/model/REbus_status"] = self.fixture("rebus_status.json")
            routes["/device/3/model/pvlink_status"] = self.fixture("pvlink_status.json")
            routes["/device/3/model/pvrss_telemetry"] = self.fixture("pvrss_telemetry.json")
            routes["/device/9/model/inverter_status"] = self.fixture("inverter_status.json")
            routes["/device/9/model/REbus_exp"] = self.fixture("rebus_exp.json")
            routes["/device/9/model/inverter"] = {"fixed": {"WH": 85411119, "W": 3830}}
            routes["/device/10/model/battery"] = self.fixture("battery.json")

            snapshot = self.collector(directory, routes).poll()

            pv = snapshot["pv_links"]["000100030001"]
            self.assertTrue(pv["connected"])
            self.assertEqual(pv["status"], "making_power")
            self.assertEqual(pv["snaprs_detected"], 12)
            self.assertFalse(pv["fault"])
            self.assertEqual(snapshot["grid"]["import_energy_kwh"], 2102.074)
            self.assertEqual(snapshot["grid"]["export_energy_kwh"], 41917.264)
            self.assertEqual(snapshot["batteries"][0]["state_of_charge_percent"], 29.9)
            self.assertEqual(snapshot["system"]["solar_power_w"], 312)
            inverter = snapshot["inverter"]
            self.assertEqual(inverter["system_operating_mode"], "Clean Backup")
            self.assertEqual(inverter["system_operating_mode_key"], "CLEAN_BACKUP")
            self.assertEqual(inverter["system_operating_mode_code"], 3)
            self.assertEqual(
                inverter["system_operating_mode_description"],
                "Charge batteries from solar only before supporting local loads and exporting to utility grid.",
            )

    def test_unknown_and_missing_system_operating_modes_remain_observable(self):
        self.assertEqual(
            decode_system_operating_mode(99),
            {
                "system_operating_mode": "Unknown (99)",
                "system_operating_mode_key": "UNKNOWN_99",
                "system_operating_mode_code": 99,
                "system_operating_mode_description": None,
            },
        )
        self.assertEqual(
            decode_system_operating_mode(None),
            {
                "system_operating_mode": None,
                "system_operating_mode_key": None,
                "system_operating_mode_code": None,
                "system_operating_mode_description": None,
            },
        )

    def test_model_failure_backs_off_without_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            routes = {"/devices": self.fixture("devices.json")}
            collector = self.collector(directory, routes, detail_interval=60)
            snapshot = collector.poll()
            pv = snapshot["pv_links"]["000100030001"]
            self.assertTrue(pv["connected"])
            self.assertIsNone(pv["fault"])
            self.assertEqual(pv["fault_summary"], "unknown")
            self.assertFalse(pv["endpoint_health"]["pvlink_status"]["available"])
            self.assertEqual(pv["endpoint_health"]["pvlink_status"]["retry_in_seconds"], 60)

    def test_model_failure_backoff_grows_and_is_capped(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000]
            routes = {"/devices": self.fixture("devices.json")}
            collector = self.collector(directory, routes, detail_interval=60)
            collector.clock = lambda: now[0]
            key = ("000100030001", "pvlink_status")
            expected_delays = [60, 120, 240, 480, 900]
            for delay in expected_delays:
                collector.poll()
                self.assertEqual(collector._endpoint_health[key]["retry_in_seconds"], delay)
                now[0] = collector._next_detail[key]

    def test_frozen_collector_reports_but_does_not_monitor_unknown_pv(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = PvInventory(str(Path(directory, "inventory.json")), frozen=True)
            collector = InstallerTelemetry(
                "http://installer",
                inventory,
                clock=lambda: 1000,
                request_get=lambda url, timeout: Response(self.fixture("devices.json"))
                if url.endswith("/devices") else Response(status=500),
            )
            snapshot = collector.poll()
            self.assertEqual(snapshot["pv_links"], {})
            self.assertEqual(snapshot["untracked_pv_links"], ["000100030001"])
            self.assertEqual(snapshot["system"]["untracked_pv_link_count"], 1)
            self.assertEqual(snapshot["system"]["untracked_pv_links"], ["000100030001"])
            self.assertEqual(snapshot["system"]["solar_power_w"], 312)

    def test_absent_or_stale_learned_string_is_disconnected_at_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            routes = {"/devices": self.fixture("devices.json")}
            collector = self.collector(directory, routes, disconnect_after=120)
            self.assertTrue(collector.poll()["pv_links"]["000100030001"]["connected"])
            routes["/devices"] = {"pv": [], "inv": [], "batt": [], "version": "1.8.2"}
            self.assertFalse(collector.poll()["pv_links"]["000100030001"]["connected"])

            routes["/devices"] = self.fixture("devices.json")
            routes["/devices"]["pv"][0]["lastheard"] = 120
            self.assertTrue(collector.poll()["pv_links"]["000100030001"]["connected"])
            routes["/devices"]["pv"][0]["lastheard"] = 121
            self.assertFalse(collector.poll()["pv_links"]["000100030001"]["connected"])

    def test_failed_devices_request_expires_api_but_preserves_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000]
            routes = {"/devices": self.fixture("devices.json")}
            collector = self.collector(directory, routes)
            collector.clock = lambda: now[0]
            self.assertTrue(collector.poll()["api_connected"])
            routes["/devices"] = requests.exceptions.Timeout("hung")
            now[0] = 1121
            snapshot = collector.poll()
            self.assertFalse(snapshot["api_connected"])
            self.assertIn("000100030001", snapshot["pv_links"])
            self.assertFalse(snapshot["pv_links"]["000100030001"]["connected"])

    def test_fault_and_unknown_status_are_separate_from_connectivity(self):
        self.assertEqual(decode_rebus_state(0x201F)["status"], "making_power")
        decoded = decode_rebus_state(0x4555)
        self.assertEqual(decoded["status"], "unknown_0x4550")
        self.assertEqual(decoded["status_severity"], "warning")

        with tempfile.TemporaryDirectory() as directory:
            routes = {"/devices": self.fixture("devices.json")}
            routes["/device/3/model/REbus_status"] = {"fixed": {"St": 0x7010}}
            routes["/device/3/model/pvlink_status"] = {"fixed": {"ErrorWord": 4}}
            routes["/device/3/model/pvrss_telemetry"] = {"fixed": {"SelfTestResults": 4}}
            routes["/device/3/model/common"] = {"fixed": {}}
            snapshot = self.collector(directory, routes).poll()
            pv = snapshot["pv_links"]["000100030001"]
            self.assertTrue(pv["connected"])
            self.assertTrue(pv["fault"])
            self.assertEqual(snapshot["system"]["faulted_string_count"], 1)


if __name__ == "__main__":
    unittest.main()
