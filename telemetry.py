"""Installer API telemetry, status decoding, and persistent PV Link inventory."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import requests


LOG = logging.getLogger(__name__)


class InventoryError(ValueError):
    """The persistent PV Link inventory cannot be used safely."""


class PvInventory:
    VERSION = 1

    def __init__(self, path: str, frozen: bool = False, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.frozen = frozen
        self.clock = clock
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    @property
    def serials(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != self.VERSION or not isinstance(payload.get("pv_links"), list):
                raise InventoryError("unsupported PV inventory format")
            for entry in payload["pv_links"]:
                serial = entry.get("serial") if isinstance(entry, dict) else None
                if not isinstance(serial, str) or not serial.strip():
                    raise InventoryError("PV inventory contains an invalid serial")
                normalized = serial.strip().upper()
                if normalized in self._entries:
                    raise InventoryError(f"PV inventory contains duplicate serial {normalized}")
                self._entries[normalized] = {
                    "serial": normalized,
                    "first_seen": int(entry.get("first_seen", 0)),
                }
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            if isinstance(error, InventoryError):
                raise
            raise InventoryError(f"cannot read PV inventory {self.path}: {error}") from error

    def observe(self, serials: list[str]) -> tuple[str, ...]:
        added = []
        if not self.frozen:
            for serial in serials:
                normalized = serial.strip().upper()
                if normalized and normalized not in self._entries:
                    self._entries[normalized] = {
                        "serial": normalized,
                        "first_seen": int(self.clock()),
                    }
                    added.append(normalized)
        if added:
            self._save()
            LOG.info("Learned PV Link%s: %s", "s" if len(added) != 1 else "", ", ".join(added))
        return tuple(added)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "pv_links": [self._entries[serial] for serial in sorted(self._entries)],
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                os.chmod(output.name, 0o600)
                json.dump(payload, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise InventoryError(f"cannot write PV inventory {self.path}: {error}") from error


REBUS_STATES = {
    0x0000: ("unknown", "unknown"),
    0x0010: ("disabled", "disabled"),
    0x0020: ("emergency_stop", "warning"),
    0x0100: ("initializing", "warning"),
    0x0110: ("powering_up", "warning"),
    0x0120: ("connecting_bus", "warning"),
    0x0130: ("disconnecting_bus", "warning"),
    0x0140: ("testing_bus", "warning"),
    0x0200: ("low_bus_voltage", "warning"),
    0x0300: ("standby", "warning"),
    0x0310: ("waiting", "warning"),
    0x0320: ("waiting_no_input", "warning"),
    0x0330: ("waiting_heartbeat", "warning"),
    0x0800: ("connecting_grid", "warning"),
    0x0810: ("disconnecting_grid", "warning"),
    0x0820: ("grid_connected", "normal"),
    0x0830: ("islanded", "normal"),
    0x1000: ("low_input", "warning"),
    0x1010: ("testing_input", "warning"),
    0x2000: ("running", "normal"),
    0x2010: ("making_power", "normal"),
    0x2020: ("limiting_power", "normal"),
    0x3000: ("low_wind", "warning"),
    0x3010: ("high_wind", "warning"),
    0x3100: ("low_sun", "low_sun"),
    0x6000: ("charging_battery", "normal"),
    0x6010: ("regulating_battery", "normal"),
    0x6020: ("charging_battery", "normal"),
    0x6100: ("discharging_battery", "normal"),
    0x6300: ("cell_imbalance", "warning"),
    0x7000: ("error", "error"),
    0x7010: ("input_over_voltage", "error"),
    0x7020: ("output_over_voltage", "error"),
    0x7030: ("input_over_current", "error"),
    0x7040: ("output_over_current", "error"),
    0x7100: ("over_temperature", "error"),
    0x8000: ("offline", "error"),
}

PVRSS_SELF_TEST_RESULTS = {
    0: "success",
    1: "voc_low",
    2: "none",
    3: "vlow_high",
    4: "count_mismatch",
    5: "vlow_timeout",
    6: "not_configured",
    7: "count_out_of_range",
    8: "vlow_low",
}
PVRSS_FAILURE_RESULTS = {1, 3, 4, 5, 7, 8}
PV_ERROR_BITS = (
    "hardware_arc_fault",
    "reverse_current",
    "input_over_current",
    "input_over_voltage",
    "ground_fault_test_failed",
    "low_input_impedance",
    "flash_crc_failed",
    "eeprom_crc_failed",
    "spt_crc_failed",
    "over_temperature",
    "dead_fet",
    "hardware_version_mismatch",
)


def decode_rebus_state(value: Any) -> dict[str, Any]:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        raw = 0
    code = raw & 0xFFF0
    name, severity = REBUS_STATES.get(
        code,
        (f"unknown_0x{code:04x}", "error" if 0x7000 <= code <= 0x7FF0 else "warning"),
    )
    return {"status": name, "status_code": code, "status_code_hex": f"0x{code:04X}", "status_severity": severity}


def _fixed(model: Any) -> dict[str, Any]:
    if isinstance(model, dict) and isinstance(model.get("fixed"), dict):
        return model["fixed"]
    return {}


def _number(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and isinstance(data[name], (int, float)) and not isinstance(data[name], bool):
            return data[name]
    return None


class InstallerTelemetry:
    """Collects installer API data and returns one normalized snapshot."""

    MODEL_INTERVAL = 60
    MAX_MODEL_BACKOFF = 900

    def __init__(
        self,
        base_url: str,
        inventory: PvInventory,
        ignored: list[str] | None = None,
        transport: Any = None,
        detail_interval: int = MODEL_INTERVAL,
        disconnect_after: int = 120,
        clock: Callable[[], float] = time.time,
        request_get: Callable[..., Any] = requests.get,
    ):
        self.base_url = base_url.rstrip("/")
        self.inventory = inventory
        self.ignored = {serial.upper() for serial in (ignored or [])}
        self.transport = transport
        self.detail_interval = detail_interval
        self.disconnect_after = disconnect_after
        self.clock = clock
        self.request_get = request_get
        self._details: dict[tuple[str, str], dict[str, Any]] = {}
        self._endpoint_health: dict[tuple[str, str], dict[str, Any]] = {}
        self._next_detail: dict[tuple[str, str], float] = {}
        self._failures: dict[tuple[str, str], int] = {}
        self._last_devices: dict[str, dict[str, Any]] = {}
        self._last_devices_success: float | None = None

    def _get_json(self, path: str) -> dict[str, Any]:
        try:
            response = self.request_get(self.base_url + path, timeout=5)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
            if self.transport:
                self.transport.report_transport_failure(error)
            raise
        if response.status_code != 200:
            raise requests.exceptions.HTTPError(f"HTTP {response.status_code} for {path}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"non-object response for {path}")
        if self.transport:
            self.transport.report_success()
        return payload

    @staticmethod
    def _kind(group: str, entry: dict[str, Any]) -> str:
        declared = str(entry.get("type", group)).lower()
        if group == "pv" or declared == "pv":
            return "pv"
        if group == "batt" or declared == "batt":
            return "battery"
        if group == "inv" or declared == "inv":
            return "inverter"
        return declared

    def _read_devices(self, now: float) -> bool:
        payload = self._get_json("/devices")
        current: dict[str, dict[str, Any]] = {}
        for group, entries in payload.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                serial = entry.get("rcpn")
                if not isinstance(serial, str) or entry.get("modID") is None:
                    continue
                normalized = serial.upper()
                item = dict(entry)
                item["serial"] = normalized
                item["kind"] = self._kind(group, item)
                try:
                    age = max(0.0, float(item.get("lastheard", 0)))
                except (TypeError, ValueError):
                    age = float(self.disconnect_after + 1)
                item["last_heard_seconds"] = age
                item["last_seen_at"] = int(now - age)
                current[normalized] = item
        self._last_devices = current
        self._last_devices_success = now
        self.inventory.observe([serial for serial, item in current.items() if item["kind"] == "pv"])
        return True

    @staticmethod
    def _models_for(kind: str) -> tuple[str, ...]:
        if kind == "inverter":
            return ("common", "REbus_status", "inverter_status", "REbus_exp", "inverter")
        if kind == "battery":
            return ("common", "REbus_status", "battery")
        if kind == "pv":
            return ("common", "REbus_status", "pvlink_status", "pvrss_telemetry")
        return ()

    def _read_details(self, now: float) -> None:
        for serial, device in self._last_devices.items():
            if serial in self.ignored:
                continue
            for model in self._models_for(device["kind"]):
                key = (serial, model)
                if now < self._next_detail.get(key, 0):
                    continue
                path = f"/device/{int(device['modID'])}/model/{model}"
                try:
                    self._details[key] = self._get_json(path)
                    self._failures[key] = 0
                    self._next_detail[key] = now + self.detail_interval
                    self._endpoint_health[key] = {"available": True, "last_success": int(now)}
                except (requests.RequestException, ValueError, TypeError) as error:
                    failures = self._failures.get(key, 0) + 1
                    self._failures[key] = failures
                    delay = min(self.MAX_MODEL_BACKOFF, self.detail_interval * (2 ** (failures - 1)))
                    self._next_detail[key] = now + delay
                    health = self._endpoint_health.setdefault(key, {})
                    health.update({"available": False, "last_error": str(error), "retry_in_seconds": delay})
                    LOG.warning("Installer model %s for %s unavailable; retrying in %ss: %s", model, serial, delay, error)

    def poll(self) -> dict[str, Any]:
        now = self.clock()
        try:
            devices_ok = self._read_devices(now)
        except (requests.RequestException, ValueError, TypeError) as error:
            devices_ok = False
            LOG.warning("Installer devices endpoint unavailable: %s", error)
        if devices_ok:
            self._read_details(now)
        return self._snapshot(now)

    def current_snapshot(self) -> dict[str, Any]:
        """Age the last observation without touching an unavailable transport."""
        return self._snapshot(self.clock())

    def _base_device(self, serial: str, kind: str, now: float) -> dict[str, Any]:
        source = self._last_devices.get(serial)
        present = source is not None
        age = max(0.0, now - source["last_seen_at"]) if source else None
        connected = bool(present and age is not None and age <= self.disconnect_after)
        power = _number(source or {}, "power")
        state = {
            "serial": serial,
            "kind": kind,
            "connected": connected,
            "present": present,
            "last_heard_seconds": age,
            "last_seen_at": source.get("last_seen_at") if source else None,
            "mod_id": source.get("modID") if source else None,
            "power_w": power,
            "input_power_w": abs(min(0, power)) if power is not None else None,
            "output_power_w": max(0, power) if power is not None else None,
            "endpoint_health": {},
        }
        for model in self._models_for(kind):
            key = (serial, model)
            if key in self._details:
                state.setdefault("raw_models", {})[model] = self._details[key]
            if key in self._endpoint_health:
                state["endpoint_health"][model] = self._endpoint_health[key]
        return state

    def _apply_rebus(self, state: dict[str, Any]) -> None:
        common = _fixed(state.get("raw_models", {}).get("common"))
        rebus = _fixed(state.get("raw_models", {}).get("REbus_status"))
        state.update(decode_rebus_state(rebus.get("St")))
        state.update({
            "firmware_version": common.get("Vr"),
            "rebus_power_w": _number(rebus, "P"),
            "accumulated_energy_kwh": (_number(rebus, "E") / 1000) if _number(rebus, "E") is not None else None,
            "voltage_v": _number(rebus, "V"),
            "current_a": _number(rebus, "I"),
            "temperature_c": _number(rebus, "T"),
            "event_code": rebus.get("Ev"),
            "rebus_bits": rebus.get("RB"),
        })

    def _pv_state(self, serial: str, now: float) -> dict[str, Any]:
        state = self._base_device(serial, "pv", now)
        self._apply_rebus(state)
        pv = _fixed(state.get("raw_models", {}).get("pvlink_status"))
        pvrss = _fixed(state.get("raw_models", {}).get("pvrss_telemetry"))
        result = pvrss.get("SelfTestResults")
        result_number = int(result) if isinstance(result, (int, float)) else None
        state.update({
            "enabled": bool(pv.get("Ena")) if "Ena" in pv else None,
            "input_voltage_v": _number(pv, "Vin"),
            "input_current_a": _number(pv, "Iin"),
            "maximum_current_a": _number(pv, "AMax"),
            "error_word": int(pv.get("ErrorWord", 0) or 0),
            "pv_status_word": pv.get("StatusWord"),
            "pvrss_status": pvrss.get("Status"),
            "pvrss_self_test": PVRSS_SELF_TEST_RESULTS.get(result_number, result),
            "snaprs_installed": _number(pvrss, "InstalledCount"),
            "snaprs_detected": _number(pvrss, "DetectedCount"),
            "number_of_strings": _number(pvrss, "NumStrings"),
            "telemetry_updated_at": pvrss.get("LastUpdatedUTCTimestamp"),
        })
        state["error_names"] = [
            name for bit, name in enumerate(PV_ERROR_BITS) if state["error_word"] & (1 << bit)
        ]
        fault_reasons = list(state["error_names"])
        if state["status_severity"] == "error":
            fault_reasons.append(state["status"])
        if result_number in PVRSS_FAILURE_RESULTS:
            fault_reasons.append(f"pvrss_{state['pvrss_self_test']}")
        if pvrss.get("LockoutError"):
            fault_reasons.append("pvrss_lockout")
        assessment_models = {"REbus_status", "pvlink_status", "pvrss_telemetry"}
        assessment_complete = assessment_models.issubset(state.get("raw_models", {}))
        state["fault_reasons"] = sorted(set(fault_reasons))
        state["fault_assessment_complete"] = assessment_complete
        state["fault"] = True if state["fault_reasons"] else (False if assessment_complete else None)
        state["fault_summary"] = (
            ", ".join(state["fault_reasons"])
            if state["fault_reasons"]
            else ("none" if assessment_complete else "unknown")
        )
        return state

    def _generic_state(self, serial: str, kind: str, now: float) -> dict[str, Any]:
        state = self._base_device(serial, kind, now)
        self._apply_rebus(state)
        models = state.get("raw_models", {})
        if kind == "battery":
            battery = _fixed(models.get("battery"))
            source = self._last_devices.get(serial, {})
            state.update({
                "state_of_charge_percent": _number(battery, "SoC") if _number(battery, "SoC") is not None else _number(source, "soc"),
                "state_of_health_percent": _number(battery, "SoH"),
                "rated_capacity_kwh": (_number(battery, "WHRtg") / 1000) if _number(battery, "WHRtg") is not None else None,
                "maximum_charge_power_w": _number(battery, "WChaMax", "MaxChaW"),
                "maximum_discharge_power_w": _number(battery, "WDisChaMax", "MaxDisChaW"),
                "minimum_cell_voltage_v": _number(battery, "CellVMin"),
                "maximum_cell_voltage_v": _number(battery, "CellVMax"),
            })
        return state

    def _snapshot(self, now: float) -> dict[str, Any]:
        api_connected = bool(
            self._last_devices_success is not None
            and now - self._last_devices_success <= self.disconnect_after
        )
        pvs = {
            serial: self._pv_state(serial, now)
            for serial in self.inventory.serials
            if serial not in self.ignored
        }
        inverters = [
            self._generic_state(serial, "inverter", now)
            for serial, item in self._last_devices.items()
            if item["kind"] == "inverter" and serial not in self.ignored
        ]
        batteries = [
            self._generic_state(serial, "battery", now)
            for serial, item in self._last_devices.items()
            if item["kind"] == "battery" and serial not in self.ignored
        ]
        inverter = inverters[0] if inverters else None
        grid = None
        if inverter:
            models = inverter.get("raw_models", {})
            status = _fixed(models.get("inverter_status"))
            expansion = _fixed(models.get("REbus_exp"))
            signed_power = _number(status, "CTPow")
            grid = {
                "power_w": signed_power,
                "import_power_w": abs(min(0, signed_power)) if signed_power is not None else None,
                "export_power_w": max(0, signed_power) if signed_power is not None else None,
                "import_energy_kwh": (_number(expansion, "Whin") / 1000) if _number(expansion, "Whin") is not None else None,
                "export_energy_kwh": (_number(expansion, "Whx") / 1000) if _number(expansion, "Whx") is not None else None,
                "raw_inverter_status": status,
                "raw_rebus_exp": expansion,
            }
        connected = sum(1 for state in pvs.values() if state["connected"])
        faulted = sum(1 for state in pvs.values() if state["fault"])
        unknown_faults = sum(1 for state in pvs.values() if state["fault"] is None)
        solar_power = sum(
            state["output_power_w"] or 0 for state in pvs.values() if state["present"]
        )
        return {
            "timestamp": int(now),
            "api_connected": api_connected,
            "system": {
                "solar_power_w": solar_power,
                "learned_string_count": len(pvs),
                "connected_string_count": connected,
                "disconnected_string_count": len(pvs) - connected,
                "faulted_string_count": faulted,
                "unknown_fault_string_count": unknown_faults,
                "any_string_disconnected": connected != len(pvs),
                "any_string_faulted": faulted > 0,
            },
            "inverter": inverter,
            "grid": grid,
            "batteries": batteries,
            "pv_links": pvs,
            "untracked_pv_links": sorted(
                serial
                for serial, item in self._last_devices.items()
                if item["kind"] == "pv" and serial not in self.inventory.serials
            ),
        }
