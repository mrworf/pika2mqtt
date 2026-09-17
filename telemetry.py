"""Installer API telemetry, status decoding, and persistent PV Link inventory."""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import random
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests


LOG = logging.getLogger(__name__)
PV_POWER_MAX_W = 5000


class InventoryError(ValueError):
    """The persistent PV Link inventory cannot be used safely."""


class OperatingModeError(RuntimeError):
    """A requested inverter operating-mode change was not confirmed."""


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

SYSTEM_OPERATING_MODES = {
    0: (
        "SAFETY_SHUTDOWN",
        "Safety Shutdown",
        "All devices disabled and DC bus de-energized.",
    ),
    1: (
        "GRID_TIE",
        "Grid Tie",
        "Support local loads and export solar power to the utility grid.",
    ),
    2: (
        "SELF_SUPPLY",
        "Self Supply",
        "Utilize both solar power and battery power to support local loads before exporting surplus solar to utility grid.",
    ),
    3: (
        "CLEAN_BACKUP",
        "Clean Backup",
        "Charge batteries from solar only before supporting local loads and exporting to utility grid.",
    ),
    4: (
        "PRIORITY_BACKUP",
        "Priority Backup",
        "Charge batteries with both solar and the utility grid.",
    ),
    5: ("REMOTE_ARBITRAGE", "Remote Arbitrage", None),
    6: (
        "SELL",
        "Sell",
        "Export full capacity, including battery power, to utility grid.",
    ),
}
WRITABLE_SYSTEM_OPERATING_MODE_CODES = frozenset({1, 2, 3, 4})
SYSTEM_OPERATING_MODE_COMMANDS = {
    SYSTEM_OPERATING_MODES[code][1]: code
    for code in sorted(WRITABLE_SYSTEM_OPERATING_MODE_CODES)
}


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


def decode_system_operating_mode(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        code = None
    else:
        try:
            code = int(value)
        except (TypeError, ValueError):
            code = None
    if code is None:
        key, label, description = None, None, None
    elif code in SYSTEM_OPERATING_MODES:
        key, label, description = SYSTEM_OPERATING_MODES[code]
    else:
        key, label, description = f"UNKNOWN_{code}", f"Unknown ({code})", None
    return {
        "system_operating_mode": label,
        "system_operating_mode_key": key,
        "system_operating_mode_code": code,
        "system_operating_mode_description": description,
    }


def _fixed(model: Any) -> dict[str, Any]:
    if isinstance(model, dict) and isinstance(model.get("fixed"), dict):
        return model["fixed"]
    return {}


def _number(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and isinstance(data[name], (int, float)) and not isinstance(data[name], bool):
            return data[name]
    return None


def _valid_pv_power(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= PV_POWER_MAX_W
    )


class InstallerTelemetry:
    """Collects installer API data and returns one normalized snapshot."""

    MODEL_INTERVAL = 60
    MODEL_STALE_AFTER = 120
    MAX_MODEL_BACKOFF = 900

    def __init__(
        self,
        base_url: str,
        inventory: PvInventory,
        ignored: list[str] | None = None,
        transport: Any = None,
        detail_interval: int = MODEL_INTERVAL,
        detail_stale_after: int = MODEL_STALE_AFTER,
        disconnect_after: int = 120,
        clock: Callable[[], float] = time.time,
        request_get: Callable[..., Any] = requests.get,
        request_post: Callable[..., Any] = requests.post,
        detail_request_get: Callable[..., Any] | None = None,
        detail_request_spacing: float = 0.25,
        retry_jitter: Callable[[float, float], float] = random.uniform,
    ):
        self.base_url = base_url.rstrip("/")
        self.inventory = inventory
        self.ignored = {serial.upper() for serial in (ignored or [])}
        self.transport = transport
        self.detail_interval = detail_interval
        self.detail_stale_after = detail_stale_after
        self.disconnect_after = disconnect_after
        self.clock = clock
        self.request_get = request_get
        self.request_post = request_post
        self._detail_session = None
        if detail_request_get is not None:
            self.detail_request_get = detail_request_get
        elif request_get is requests.get:
            self._detail_session = requests.Session()
            self.detail_request_get = self._detail_session.get
        else:
            self.detail_request_get = request_get
        self.detail_request_spacing = detail_request_spacing
        self.retry_jitter = retry_jitter
        self._lock = threading.RLock()
        self._detail_request_lock = threading.Lock()
        self._detail_stop = threading.Event()
        self._detail_wake = threading.Event()
        self._detail_thread: threading.Thread | None = None
        self._details: dict[tuple[str, str], dict[str, Any]] = {}
        self._endpoint_health: dict[tuple[str, str], dict[str, Any]] = {}
        self._next_detail: dict[tuple[str, str], float] = {}
        self._failures: dict[tuple[str, str], int] = {}
        self._last_devices: dict[str, dict[str, Any]] = {}
        self._last_devices_success: float | None = None
        self._invalid_pv_power_serials: set[str] = set()

    def _get_json(
        self,
        path: str,
        request_get: Callable[..., Any] | None = None,
        report_transport: bool = True,
    ) -> dict[str, Any]:
        getter = request_get or self.request_get
        try:
            response = getter(self.base_url + path, timeout=5)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
            if report_transport and self.transport:
                self.transport.report_transport_failure(error)
            raise
        if response.status_code != 200:
            raise requests.exceptions.HTTPError(f"HTTP {response.status_code} for {path}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"non-object response for {path}")
        if report_transport and self.transport:
            self.transport.report_success()
        return payload

    def _post_form(self, path: str, data: dict[str, str]) -> None:
        try:
            response = self.request_post(self.base_url + path, data=data, timeout=5)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
            if self.transport:
                self.transport.report_transport_failure(error)
            raise
        if not 200 <= response.status_code < 300:
            raise requests.exceptions.HTTPError(f"HTTP {response.status_code} for {path}")
        if self.transport:
            self.transport.report_success()

    def set_system_operating_mode(self, code: int) -> dict[str, Any]:
        if (
            not isinstance(code, int)
            or isinstance(code, bool)
            or code not in WRITABLE_SYSTEM_OPERATING_MODE_CODES
        ):
            raise OperatingModeError(f"operating mode code {code!r} is not writable")
        with self._lock:
            inverter = next(
                (
                    (serial, dict(device))
                    for serial, device in self._last_devices.items()
                    if device["kind"] == "inverter" and serial not in self.ignored
                ),
                None,
            )
        if inverter is None:
            raise OperatingModeError("no inverter is currently available")
        serial, device = inverter
        path = f"/device/{int(device['modID'])}/model/inverter_status"
        try:
            with self._detail_request_lock:
                self._post_form(path, {"SysMd": str(code)})
                readback = self._get_json(path)
        except (requests.RequestException, ValueError, TypeError) as error:
            raise OperatingModeError(f"operating mode request failed: {error}") from error
        confirmed = _fixed(readback).get("SysMd")
        if isinstance(confirmed, bool) or not isinstance(confirmed, (int, float)):
            raise OperatingModeError("operating mode readback did not contain a numeric SysMd")
        if confirmed != code:
            raise OperatingModeError(
                f"operating mode readback was {confirmed!r}, expected {code}"
            )

        now = self.clock()
        key = (serial, "inverter_status")
        with self._lock:
            current = self._last_devices.get(serial)
            if current is None or int(current["modID"]) != int(device["modID"]):
                raise OperatingModeError(
                    "inverter mapping changed before operating mode confirmation"
                )
            self._record_detail_success(key, readback, now)
            return self._snapshot(now)

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
        with self._lock:
            self._last_devices = current
            self._last_devices_success = now
            self.inventory.observe(
                [serial for serial, item in current.items() if item["kind"] == "pv"]
            )
        self._detail_wake.set()
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

    def _record_detail_success(
        self,
        key: tuple[str, str],
        payload: dict[str, Any],
        now: float,
    ) -> None:
        failures = self._failures.get(key, 0)
        self._details[key] = payload
        self._failures[key] = 0
        self._next_detail[key] = now + self.detail_interval
        self._endpoint_health[key] = {"available": True, "last_success": int(now)}
        if failures:
            LOG.info("Installer model %s for %s recovered", key[1], key[0])

    def _record_detail_failure(
        self,
        key: tuple[str, str],
        error: BaseException,
        now: float,
        stagger: bool,
    ) -> None:
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        base_delay = min(
            self.MAX_MODEL_BACKOFF,
            self.detail_interval * (2 ** (failures - 1)),
        )
        jitter_limit = min(15.0, base_delay * 0.25) if stagger else 0.0
        jitter = self.retry_jitter(0.0, jitter_limit) if jitter_limit else 0.0
        delay = min(self.MAX_MODEL_BACKOFF, base_delay + jitter)
        self._next_detail[key] = now + delay
        health = self._endpoint_health.setdefault(key, {})
        health.update(
            {
                "available": False,
                "last_error": str(error),
                "retry_in_seconds": delay,
            }
        )
        LOG.warning(
            "Installer model %s for %s unavailable; retrying in %.1fs: %s",
            key[1],
            key[0],
            delay,
            error,
        )

    def _read_details(self, now: float) -> None:
        with self._lock:
            devices = [
                (serial, dict(device))
                for serial, device in self._last_devices.items()
            ]
        for serial, device in devices:
            if serial in self.ignored:
                continue
            for model in self._models_for(device["kind"]):
                key = (serial, model)
                with self._lock:
                    next_detail = self._next_detail.get(key, 0)
                if now < next_detail:
                    continue
                path = f"/device/{int(device['modID'])}/model/{model}"
                try:
                    with self._detail_request_lock:
                        payload = self._get_json(
                            path,
                            request_get=self.detail_request_get,
                            report_transport=False,
                        )
                except (requests.RequestException, ValueError, TypeError) as error:
                    with self._lock:
                        self._record_detail_failure(key, error, now, stagger=False)
                else:
                    with self._lock:
                        self._record_detail_success(key, payload, now)

    def _next_detail_task(
        self, now: float
    ) -> tuple[tuple[str, int, str] | None, float]:
        with self._lock:
            candidates = []
            for serial, device in self._last_devices.items():
                if serial in self.ignored:
                    continue
                for model in self._models_for(device["kind"]):
                    due = self._next_detail.get((serial, model), 0.0)
                    candidates.append(
                        (due, serial, int(device["modID"]), model)
                    )
        if not candidates:
            return None, 1.0
        due, serial, mod_id, model = min(candidates)
        if due > now:
            return None, min(1.0, due - now)
        return (serial, mod_id, model), 0.0

    def _run_detail_task(self, task: tuple[str, int, str]) -> None:
        serial, mod_id, model = task
        key = (serial, model)
        path = f"/device/{mod_id}/model/{model}"
        try:
            with self._detail_request_lock:
                payload = self._get_json(
                    path,
                    request_get=self.detail_request_get,
                    report_transport=False,
                )
            error = None
        except (requests.RequestException, ValueError, TypeError) as caught:
            payload = None
            error = caught
        now = self.clock()
        with self._lock:
            current = self._last_devices.get(serial)
            if current is None or int(current["modID"]) != mod_id:
                LOG.info(
                    "Discarding installer model %s for stale device mapping %s/%s",
                    model,
                    serial,
                    mod_id,
                )
                return
            if error is not None:
                self._record_detail_failure(key, error, now, stagger=True)
            else:
                self._record_detail_success(key, payload, now)

    def _detail_loop(self) -> None:
        LOG.info("Starting installer detail collector")
        while not self._detail_stop.is_set():
            if self.transport and hasattr(self.transport, "wait_available"):
                if not self.transport.wait_available(timeout=1):
                    continue
            task, wait_for = self._next_detail_task(self.clock())
            if task is None:
                self._detail_wake.wait(wait_for)
                self._detail_wake.clear()
                continue
            self._run_detail_task(task)
            self._detail_stop.wait(self.detail_request_spacing)
        LOG.info("Installer detail collector stopped")

    def start_detail_worker(self) -> None:
        if self._detail_thread and self._detail_thread.is_alive():
            return
        self._detail_stop.clear()
        self._detail_thread = threading.Thread(
            target=self._detail_loop,
            name="installer-detail-collector",
            daemon=True,
        )
        self._detail_thread.start()

    def stop_detail_worker(self) -> None:
        self._detail_stop.set()
        self._detail_wake.set()
        if (
            self._detail_thread
            and self._detail_thread is not threading.current_thread()
        ):
            self._detail_thread.join(timeout=10)
        self._detail_thread = None
        if self._detail_session is not None:
            self._detail_session.close()

    def _poll_devices(self, now: float) -> bool:
        try:
            return self._read_devices(now)
        except (requests.RequestException, ValueError, TypeError) as error:
            LOG.warning("Installer devices endpoint unavailable: %s", error)
            return False

    def poll(self) -> dict[str, Any]:
        now = self.clock()
        devices_ok = self._poll_devices(now)
        if devices_ok:
            self._read_details(now)
        return self._snapshot(now)

    def poll_primary(self) -> dict[str, Any]:
        """Refresh `/devices` without waiting for detailed model requests."""
        now = self.clock()
        self._poll_devices(now)
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
            health = dict(self._endpoint_health.get(key, {}))
            last_success = health.get("last_success")
            age = max(0.0, now - last_success) if last_success is not None else None
            health["data_age_seconds"] = age
            health["fresh"] = bool(
                age is not None and age <= self.detail_stale_after
            )
            state["endpoint_health"][model] = health
        return state

    @staticmethod
    def _fresh_fixed(state: dict[str, Any], model: str) -> dict[str, Any]:
        if not state.get("endpoint_health", {}).get(model, {}).get("fresh"):
            return {}
        return _fixed(state.get("raw_models", {}).get(model))

    def _apply_rebus(self, state: dict[str, Any]) -> None:
        common = self._fresh_fixed(state, "common")
        rebus = self._fresh_fixed(state, "REbus_status")
        if common:
            state["firmware_version"] = common.get("Vr")
        if rebus:
            state.update(decode_rebus_state(rebus.get("St")))
            state.update({
                "rebus_power_w": _number(rebus, "P"),
                "accumulated_energy_kwh": (_number(rebus, "E") / 1000) if _number(rebus, "E") is not None else None,
                "voltage_v": _number(rebus, "V"),
                "current_a": _number(rebus, "I"),
                "temperature_c": _number(rebus, "T"),
                "event_code": rebus.get("Ev"),
                "rebus_bits": rebus.get("RB"),
            })

    def _pv_state(self, serial: str, now: float) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
        state = self._base_device(serial, "pv", now)
        issues = []
        source = self._last_devices.get(serial)
        raw_power = source.get("power") if source else None
        if source is None or not _valid_pv_power(raw_power):
            state.pop("power_w", None)
            state.pop("input_power_w", None)
            state.pop("output_power_w", None)
            if source is not None:
                issues.append(("devices.power", raw_power))

        # PV raw models are published as diagnostics, so work on a copy before
        # removing an impossible detailed power value from the public snapshot.
        if "raw_models" in state:
            state["raw_models"] = copy.deepcopy(state["raw_models"])
        rebus = self._fresh_fixed(state, "REbus_status")
        invalid_rebus_power = source is not None and "P" in rebus and not _valid_pv_power(rebus["P"])
        if invalid_rebus_power:
            issues.append(("REbus_status.P", rebus["P"]))
            del rebus["P"]
        self._apply_rebus(state)
        if invalid_rebus_power:
            state.pop("rebus_power_w", None)
        pv = self._fresh_fixed(state, "pvlink_status")
        pvrss = self._fresh_fixed(state, "pvrss_telemetry")
        result = pvrss.get("SelfTestResults")
        result_number = int(result) if isinstance(result, (int, float)) else None
        if pv:
            state.update({
                "enabled": bool(pv.get("Ena")) if "Ena" in pv else None,
                "input_voltage_v": _number(pv, "Vin"),
                "input_current_a": _number(pv, "Iin"),
                "maximum_current_a": _number(pv, "AMax"),
                "error_word": int(pv.get("ErrorWord", 0) or 0),
                "pv_status_word": pv.get("StatusWord"),
            })
        else:
            state["error_word"] = 0
        if pvrss:
            state.update({
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
        if state.get("status_severity") == "error":
            fault_reasons.append(state["status"])
        if result_number in PVRSS_FAILURE_RESULTS:
            fault_reasons.append(f"pvrss_{state['pvrss_self_test']}")
        if pvrss.get("LockoutError"):
            fault_reasons.append("pvrss_lockout")
        assessment_models = {"REbus_status", "pvlink_status", "pvrss_telemetry"}
        assessment_complete = all(
            state["endpoint_health"][model]["fresh"] for model in assessment_models
        )
        state["fault_reasons"] = sorted(set(fault_reasons))
        state["fault_assessment_complete"] = assessment_complete
        state["fault"] = True if state["fault_reasons"] else (False if assessment_complete else None)
        state["fault_summary"] = (
            ", ".join(state["fault_reasons"])
            if state["fault_reasons"]
            else ("none" if assessment_complete else "unknown")
        )
        return state, issues

    def _update_pv_power_warnings(
        self,
        issues: dict[str, list[tuple[str, Any]]],
        visible_serials: set[str],
    ) -> None:
        invalid_serials = set(issues)
        for serial in sorted(invalid_serials.difference(self._invalid_pv_power_serials)):
            details = ", ".join(f"{source}={value!r}" for source, value in issues[serial])
            LOG.warning(
                "Rejecting invalid PV power for %s (%s); accepted range is 0-%s W",
                serial,
                details,
                PV_POWER_MAX_W,
            )
        for serial in sorted(
            self._invalid_pv_power_serials.difference(invalid_serials).intersection(visible_serials)
        ):
            LOG.info("PV power for %s recovered to a valid sample", serial)
        self._invalid_pv_power_serials = invalid_serials

    def _generic_state(self, serial: str, kind: str, now: float) -> dict[str, Any]:
        state = self._base_device(serial, kind, now)
        self._apply_rebus(state)
        models = state.get("raw_models", {})
        if kind == "inverter":
            inverter_status = self._fresh_fixed(state, "inverter_status")
            if inverter_status:
                state.update(decode_system_operating_mode(inverter_status.get("SysMd")))
        if kind == "battery":
            battery = self._fresh_fixed(state, "battery")
            source = self._last_devices.get(serial, {})
            state["state_of_charge_percent"] = (
                _number(battery, "SoC")
                if _number(battery, "SoC") is not None
                else _number(source, "soc")
            )
            if battery:
                state.update({
                    "state_of_health_percent": _number(battery, "SoH"),
                    "rated_capacity_kwh": (_number(battery, "WHRtg") / 1000) if _number(battery, "WHRtg") is not None else None,
                    "maximum_charge_power_w": _number(battery, "WChaMax", "MaxChaW"),
                    "maximum_discharge_power_w": _number(battery, "WDisChaMax", "MaxDisChaW"),
                    "minimum_cell_voltage_v": _number(battery, "CellVMin"),
                    "maximum_cell_voltage_v": _number(battery, "CellVMax"),
                })
        return state

    def _snapshot(self, now: float) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(now)

    def _snapshot_locked(self, now: float) -> dict[str, Any]:
        api_connected = bool(
            self._last_devices_success is not None
            and now - self._last_devices_success <= self.disconnect_after
        )
        pvs = {}
        power_issues: dict[str, list[tuple[str, Any]]] = {}
        for serial in self.inventory.serials:
            if serial in self.ignored:
                continue
            state, issues = self._pv_state(serial, now)
            pvs[serial] = state
            if issues:
                power_issues[serial] = issues
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
            status = self._fresh_fixed(inverter, "inverter_status")
            expansion = self._fresh_fixed(inverter, "REbus_exp")
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
        visible_pv_serials = {
            serial
            for serial, item in self._last_devices.items()
            if item["kind"] == "pv" and serial not in self.ignored
        }
        for serial in visible_pv_serials:
            raw_power = self._last_devices[serial].get("power")
            if not _valid_pv_power(raw_power):
                issue = ("devices.power", raw_power)
                if issue not in power_issues.setdefault(serial, []):
                    power_issues[serial].append(issue)
        self._update_pv_power_warnings(power_issues, visible_pv_serials)
        untracked = sorted(visible_pv_serials.difference(self.inventory.serials))
        primary_power_valid = all(
            _valid_pv_power(self._last_devices[serial].get("power"))
            for serial in visible_pv_serials
        )
        system = {
            "learned_string_count": len(pvs),
            "connected_string_count": connected,
            "disconnected_string_count": len(pvs) - connected,
            "faulted_string_count": faulted,
            "unknown_fault_string_count": unknown_faults,
            "any_string_disconnected": connected != len(pvs),
            "any_string_faulted": faulted > 0,
            "untracked_pv_link_count": len(untracked),
            "untracked_pv_links": untracked,
        }
        if primary_power_valid:
            system["solar_power_w"] = sum(
                self._last_devices[serial]["power"] for serial in visible_pv_serials
            )
        return {
            "timestamp": int(now),
            "api_connected": api_connected,
            "system": system,
            "inverter": inverter,
            "grid": grid,
            "batteries": batteries,
            "pv_links": pvs,
            "untracked_pv_links": untracked,
        }
