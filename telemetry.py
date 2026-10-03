"""Installer API telemetry, status decoding, and persistent PV Link inventory."""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import random
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests
from register_definitions import RegisterDefinitions


LOG = logging.getLogger(__name__)
PV_POWER_MAX_W = 5000
MAX_BATTERY_MODULES = 32


class InventoryError(ValueError):
    """The persistent PV Link inventory cannot be used safely."""


class OperatingModeError(RuntimeError):
    """A requested inverter operating-mode change was not confirmed."""


class PvLinkControlError(RuntimeError):
    """A requested PV Link enable change could not be applied safely."""


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
    """Raw-only compatibility helper; firmware interpretation needs definitions."""
    try:
        raw = int(value)
    except (TypeError, ValueError):
        raw = 0
    return {"status": f"unknown_0x{raw:04x}", "status_code": raw, "status_code_hex": f"0x{raw:04X}", "status_severity": None}


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
    MAX_MODEL_BACKOFF_JITTER = 60
    SLOW_DETAIL_REQUEST_SECONDS = 5.0

    def __init__(
        self,
        base_url: str,
        inventory: PvInventory,
        ignored: list[str] | None = None,
        transport: Any = None,
        detail_interval: int = MODEL_INTERVAL,
        detail_stale_after: int = MODEL_STALE_AFTER,
        detail_request_timeout: float = 20.0,
        disconnect_after: int = 120,
        clock: Callable[[], float] = time.time,
        request_get: Callable[..., Any] = requests.get,
        request_post: Callable[..., Any] = requests.post,
        detail_request_get: Callable[..., Any] | None = None,
        detail_request_spacing: float = 0.25,
        retry_jitter: Callable[[float, float], float] = random.uniform,
        operating_mode_confirmation_interval: float = 5.0,
        operating_mode_confirmation_timeout: float = 30.0,
        sleeper: Callable[[float], None] = time.sleep,
        definitions: Any = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.inventory = inventory
        self.ignored = {serial.upper() for serial in (ignored or [])}
        self.transport = transport
        self.definitions = definitions if definitions is not None else RegisterDefinitions(transport)
        self.detail_interval = detail_interval
        self.detail_stale_after = detail_stale_after
        if (
            isinstance(detail_request_timeout, bool)
            or not isinstance(detail_request_timeout, (int, float))
            or not math.isfinite(detail_request_timeout)
            or detail_request_timeout < 1
        ):
            raise ValueError("detail request timeout must be at least 1 second")
        self.detail_request_timeout = float(detail_request_timeout)
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
        if operating_mode_confirmation_interval <= 0:
            raise ValueError("operating mode confirmation interval must be positive")
        if operating_mode_confirmation_timeout < 0:
            raise ValueError("operating mode confirmation timeout cannot be negative")
        self.operating_mode_confirmation_interval = operating_mode_confirmation_interval
        self.operating_mode_confirmation_timeout = operating_mode_confirmation_timeout
        self.sleeper = sleeper
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
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        getter = request_get or self.request_get
        try:
            response = getter(self.base_url + path, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
            if report_transport and self.transport:
                self.transport.report_transport_failure(error)
            raise
        if response.status_code != 200:
            raise requests.exceptions.HTTPError(
                f"HTTP {response.status_code} for {path}", response=response
            )
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

    @staticmethod
    def _operating_mode_code(payload: Any) -> int | None:
        value = _fixed(payload).get("SysMd")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and int(value) == value
        ):
            return int(value)
        return None

    @staticmethod
    def _device_mod_id(device: dict[str, Any]) -> int | None:
        try:
            value = device["modID"]
            if isinstance(value, bool):
                return None
            return int(value)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    def _operating_mode_devices(
        self,
    ) -> tuple[tuple[str, dict[str, Any]], tuple[str, dict[str, Any]]]:
        with self._lock:
            controllers = [
                (serial, dict(device))
                for serial, device in self._last_devices.items()
                if device["kind"] == "lcm" and self._device_mod_id(device) == 1
            ]
            inverters = [
                (serial, dict(device))
                for serial, device in self._last_devices.items()
                if device["kind"] == "inverter" and serial not in self.ignored
            ]
        if len(controllers) != 1:
            raise OperatingModeError(
                "exactly one LCM system controller at device ID 1 is required"
            )
        if len(inverters) != 1:
            raise OperatingModeError("exactly one inverter is required")
        return controllers[0], inverters[0]

    def set_system_operating_mode(self, code: int) -> dict[str, Any]:
        if (
            not isinstance(code, int)
            or isinstance(code, bool)
            or code not in WRITABLE_SYSTEM_OPERATING_MODE_CODES
        ):
            raise OperatingModeError(f"operating mode code {code!r} is not writable")
        controller, inverter = self._operating_mode_devices()
        controller_serial, controller_device = controller
        inverter_serial, inverter_device = inverter
        inverter_mod_id = self._device_mod_id(inverter_device)
        if inverter_mod_id is None:
            raise OperatingModeError("inverter has an invalid device ID")
        controller_path = "/device/1/model/REbus_dir"
        status_path = f"/device/{inverter_mod_id}/model/inverter_status"
        attempts = int(
            self.operating_mode_confirmation_timeout
            // self.operating_mode_confirmation_interval
        ) + 1
        controller_value: int | str | None = None
        status_value: int | str | None = None
        confirmed_status = None
        try:
            with self._detail_request_lock:
                self._post_form(controller_path, {"0_SysMd": str(code)})
                for attempt in range(attempts):
                    try:
                        controller_readback = self._get_json(controller_path)
                        controller_value = self._operating_mode_code(controller_readback)
                        if controller_value is None:
                            controller_value = "invalid"
                    except (requests.RequestException, ValueError, TypeError) as error:
                        controller_value = f"error: {error}"
                    try:
                        status_readback = self._get_json(status_path)
                        status_value = self._operating_mode_code(status_readback)
                        if status_value is None:
                            status_value = "invalid"
                    except (requests.RequestException, ValueError, TypeError) as error:
                        status_value = f"error: {error}"
                        status_readback = None
                    if controller_value == code and status_value == code:
                        confirmed_status = status_readback
                        break
                    if attempt + 1 < attempts:
                        self.sleeper(self.operating_mode_confirmation_interval)
        except (requests.RequestException, ValueError, TypeError) as error:
            raise OperatingModeError(f"operating mode request failed: {error}") from error
        if confirmed_status is None:
            raise OperatingModeError(
                "operating mode was not confirmed within "
                f"{self.operating_mode_confirmation_timeout:g} seconds "
                f"(requested={code}, controller={controller_value!r}, "
                f"inverter={status_value!r})"
            )

        now = self.clock()
        key = (inverter_serial, "inverter_status")
        with self._lock:
            current_controller = self._last_devices.get(controller_serial)
            current_inverter = self._last_devices.get(inverter_serial)
            if (
                current_controller is None
                or self._device_mod_id(current_controller) != 1
                or current_inverter is None
                or self._device_mod_id(current_inverter) != inverter_mod_id
            ):
                raise OperatingModeError(
                    "device mapping changed before operating mode confirmation"
                )
            self._record_detail_success(key, confirmed_status, now)
            return self._snapshot(now)

    @staticmethod
    def _directory_integer(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if isinstance(value, float) and (not math.isfinite(value) or value != number):
            return None
        return number

    def _pv_link_directory_entry_locked(
        self, serial: str, payload: Any
    ) -> tuple[str, int, dict[str, Any]]:
        if not isinstance(serial, str):
            raise PvLinkControlError(f"PV Link serial {serial!r} is invalid")
        normalized = serial.upper()
        if normalized in self.ignored:
            raise PvLinkControlError(f"PV Link {normalized} is ignored")
        if not re.fullmatch(r"[0-9A-F]{12}", normalized):
            raise PvLinkControlError(f"PV Link serial {serial!r} is invalid")

        controllers = [
            (candidate, device)
            for candidate, device in self._last_devices.items()
            if device.get("kind") == "lcm" and self._device_mod_id(device) == 1
        ]
        if len(controllers) != 1:
            raise PvLinkControlError(
                "exactly one LCM system controller at device ID 1 is required"
            )
        now = self.clock()
        if (
            self._last_devices_success is None
            or now - self._last_devices_success > self.disconnect_after
        ):
            raise PvLinkControlError("installer device inventory is stale")
        inverters = [
            candidate
            for candidate, candidate_device in self._last_devices.items()
            if candidate_device.get("kind") == "inverter"
            and candidate not in self.ignored
        ]
        if len(inverters) != 1:
            raise PvLinkControlError("exactly one current inverter is required")
        device = self._last_devices.get(normalized)
        if device is None or device.get("kind") != "pv":
            raise PvLinkControlError(f"PV Link {normalized} is not currently present")
        last_seen = device.get("last_seen_at")
        if (
            not isinstance(last_seen, (int, float))
            or now - last_seen > self.disconnect_after
        ):
            raise PvLinkControlError(f"PV Link {normalized} is disconnected")
        unit_id = self._device_mod_id(device)
        if unit_id is None:
            raise PvLinkControlError(f"PV Link {normalized} has an invalid device ID")

        repeating = payload.get("repeating") if isinstance(payload, dict) else None
        if not isinstance(repeating, dict):
            raise PvLinkControlError("controller device directory is malformed")
        manufacturer = int(normalized[0:4], 16)
        device_type = int(normalized[4:8], 16)
        device_id = int(normalized[8:12], 16)
        matches: list[tuple[int, dict[str, Any]]] = []
        for raw_block, entry in repeating.items():
            try:
                block = int(raw_block)
            except (TypeError, ValueError):
                continue
            if block <= 0 or str(block) != str(raw_block) or not isinstance(entry, dict):
                continue
            identity = (
                self._directory_integer(entry.get("Man")),
                self._directory_integer(entry.get("Dev")),
                self._directory_integer(entry.get("ID")),
                self._directory_integer(entry.get("UnitID")),
            )
            if identity != (manufacturer, device_type, device_id, unit_id):
                continue
            enabled = self._directory_integer(entry.get("Ena"))
            if enabled not in (0, 1):
                raise PvLinkControlError(
                    f"controller directory has an invalid Ena value for {normalized}"
                )
            matches.append((block, dict(entry)))
        if len(matches) != 1:
            raise PvLinkControlError(
                f"controller directory has {len(matches)} matches for PV Link {normalized}"
            )
        block, entry = matches[0]
        return controllers[0][0], block, entry

    def _pv_link_directory_match_locked(
        self, serial: str, payload: Any
    ) -> tuple[str, int, bool]:
        controller, block, entry = self._pv_link_directory_entry_locked(
            serial, payload
        )
        return controller, block, bool(self._directory_integer(entry.get("Ena")))

    def _pv_link_directory_match(
        self, serial: str, payload: Any
    ) -> tuple[str, int, bool]:
        with self._lock:
            return self._pv_link_directory_match_locked(serial, payload)

    def _fresh_pv_link_directory_match(
        self, serial: str, now: float
    ) -> tuple[int, bool] | None:
        controllers = [
            candidate
            for candidate, device in self._last_devices.items()
            if device.get("kind") == "lcm" and self._device_mod_id(device) == 1
        ]
        if len(controllers) != 1:
            return None
        key = (controllers[0], "REbus_dir")
        health = self._endpoint_health.get(key, {})
        last_success = health.get("last_success")
        if last_success is None or now - last_success > self.detail_stale_after:
            return None
        try:
            _, block, enabled = self._pv_link_directory_match_locked(
                serial, self._details.get(key)
            )
        except PvLinkControlError:
            return None
        return block, enabled

    def _fresh_pv_link_directory_entry(
        self, serial: str, now: float
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        controllers = [
            candidate
            for candidate, device in self._last_devices.items()
            if device.get("kind") == "lcm" and self._device_mod_id(device) == 1
        ]
        if len(controllers) != 1:
            return None, {"available": False, "fresh": False}
        key = (controllers[0], "REbus_dir")
        health = dict(self._endpoint_health.get(key, {}))
        last_success = health.get("last_success")
        age = max(0.0, now - last_success) if last_success is not None else None
        health["data_age_seconds"] = age
        health["fresh"] = bool(
            age is not None and age <= self.detail_stale_after
        )
        if not health["fresh"]:
            return None, health
        try:
            _, _, entry = self._pv_link_directory_entry_locked(
                serial, self._details.get(key)
            )
        except PvLinkControlError as error:
            health.update({"available": False, "fresh": False, "last_error": str(error)})
            return None, health
        return entry, health

    def set_pv_link_enabled(self, serial: str, enabled: bool) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise PvLinkControlError("PV Link enabled state must be boolean")
        if not isinstance(serial, str):
            raise PvLinkControlError(f"PV Link serial {serial!r} is invalid")
        normalized = serial.upper()
        path = "/device/1/model/REbus_dir/devices"
        attempts = int(
            self.operating_mode_confirmation_timeout
            // self.operating_mode_confirmation_interval
        ) + 1
        last_observed: bool | str | None = None
        confirmed_payload = None
        controller_serial = None
        original_block = None
        try:
            with self._detail_request_lock:
                initial = self._get_json(path)
                controller_serial, original_block, current = (
                    self._pv_link_directory_match(normalized, initial)
                )
                if current == enabled:
                    confirmed_payload = initial
                else:
                    self._post_form(
                        path,
                        {f"{original_block}_Ena": "1" if enabled else "0"},
                    )
                    for attempt in range(attempts):
                        try:
                            readback = self._get_json(path)
                            current_controller, block, observed = (
                                self._pv_link_directory_match(normalized, readback)
                            )
                            if (
                                current_controller != controller_serial
                                or block != original_block
                            ):
                                last_observed = "mapping changed"
                            else:
                                last_observed = observed
                                if observed == enabled:
                                    confirmed_payload = readback
                                    break
                        except (
                            requests.RequestException,
                            PvLinkControlError,
                            ValueError,
                            TypeError,
                        ) as error:
                            last_observed = f"error: {error}"
                        if attempt + 1 < attempts:
                            self.sleeper(self.operating_mode_confirmation_interval)
        except PvLinkControlError:
            raise
        except (requests.RequestException, ValueError, TypeError) as error:
            raise PvLinkControlError(f"PV Link request failed: {error}") from error

        if confirmed_payload is None:
            raise PvLinkControlError(
                "PV Link state was not confirmed within "
                f"{self.operating_mode_confirmation_timeout:g} seconds "
                f"(serial={normalized}, requested={enabled}, "
                f"last_observed={last_observed!r})"
            )

        now = self.clock()
        with self._lock:
            current_controller, block, observed = self._pv_link_directory_match_locked(
                normalized, confirmed_payload
            )
            if (
                current_controller != controller_serial
                or block != original_block
                or observed != enabled
            ):
                raise PvLinkControlError(
                    "PV Link mapping changed before state confirmation"
                )
            self._record_detail_success(
                (controller_serial, "REbus_dir"), confirmed_payload, now
            )
            return self._snapshot_locked(now)

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
            return ("common", "REbus_status", "battery", "lithium_ion_string")
        if kind == "pv":
            return ("common", "REbus_status", "pvlink_status", "pvrss_telemetry")
        if kind == "lcm":
            return ("REbus_dir",)
        return ()

    @staticmethod
    def _model_path(mod_id: int, model: str) -> str:
        path = f"/device/{int(mod_id)}/model/{model}"
        if model == "lithium_ion_string":
            path += "/lithium_ion_string_module"
        elif model == "REbus_dir":
            path += "/devices"
        return path

    def _record_detail_success(
        self,
        key: tuple[str, str],
        payload: dict[str, Any],
        now: float,
        duration: float | None = None,
    ) -> None:
        failures = self._failures.get(key, 0)
        self._details[key] = payload
        self._failures[key] = 0
        self._next_detail[key] = now + self.detail_interval
        health = {
            "available": True,
            "last_success": int(now),
            "last_attempt": int(now),
            "consecutive_failures": 0,
            "next_retry_at": None,
        }
        if duration is not None:
            health["last_request_duration_seconds"] = round(duration, 3)
        self._endpoint_health[key] = health
        if failures:
            LOG.info(
                "Installer model %s for %s recovered%s",
                key[1],
                key[0],
                f" in {duration:.1f}s" if duration is not None else "",
            )
        elif duration is not None and duration > self.SLOW_DETAIL_REQUEST_SECONDS:
            LOG.info(
                "Slow installer model %s for %s completed in %.1fs",
                key[1],
                key[0],
                duration,
            )

    @staticmethod
    def _detail_failure_kind(error: BaseException) -> str:
        if isinstance(error, requests.exceptions.Timeout):
            return "timeout"
        if isinstance(error, requests.exceptions.ConnectionError):
            return "connection"
        if isinstance(error, requests.exceptions.HTTPError):
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            return f"http_{status}" if status is not None else "http"
        if isinstance(error, ValueError):
            return "invalid_response"
        return type(error).__name__.lower()

    def _record_detail_failure(
        self,
        key: tuple[str, str],
        error: BaseException,
        now: float,
        stagger: bool,
        duration: float | None = None,
    ) -> None:
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        base_delay = min(
            self.MAX_MODEL_BACKOFF,
            self.detail_interval * (2 ** (failures - 1)),
        )
        if stagger and base_delay >= self.MAX_MODEL_BACKOFF:
            jitter_limit = self.MAX_MODEL_BACKOFF_JITTER
        else:
            jitter_limit = min(15.0, base_delay * 0.25) if stagger else 0.0
        jitter = self.retry_jitter(0.0, jitter_limit) if jitter_limit else 0.0
        delay = base_delay + jitter
        self._next_detail[key] = now + delay
        health = self._endpoint_health.setdefault(key, {})
        failure_kind = self._detail_failure_kind(error)
        error_text = str(error)
        should_warn = (
            health.get("available") is not False
            or health.get("failure_kind") != failure_kind
            or health.get("last_error") != error_text
        )
        health.update(
            {
                "available": False,
                "last_error": error_text,
                "failure_kind": failure_kind,
                "last_attempt": int(now),
                "consecutive_failures": failures,
                "retry_in_seconds": delay,
                "next_retry_at": now + delay,
            }
        )
        if duration is not None:
            health["last_request_duration_seconds"] = round(duration, 3)
        log = LOG.warning if should_warn else LOG.debug
        log(
            "Installer model %s for %s unavailable after %.1fs; "
            "retrying in %.1fs: %s",
            key[1],
            key[0],
            duration or 0.0,
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
                path = self._model_path(device["modID"], model)
                started = time.monotonic()
                try:
                    with self._detail_request_lock:
                        payload = self._get_json(
                            path,
                            request_get=self.detail_request_get,
                            report_transport=False,
                            timeout=self.detail_request_timeout,
                        )
                except (requests.RequestException, ValueError, TypeError) as error:
                    with self._lock:
                        self._record_detail_failure(
                            key,
                            error,
                            now,
                            stagger=False,
                            duration=time.monotonic() - started,
                        )
                else:
                    with self._lock:
                        self._record_detail_success(
                            key, payload, now, duration=time.monotonic() - started
                        )

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
                    priority = 0 if model == "REbus_dir" else 1
                    candidates.append(
                        (due, priority, serial, int(device["modID"]), model)
                    )
        if not candidates:
            return None, 1.0
        due_candidates = [candidate for candidate in candidates if candidate[0] <= now]
        if not due_candidates:
            due = min(candidate[0] for candidate in candidates)
            return None, min(1.0, due - now)
        due, _, serial, mod_id, model = min(
            due_candidates, key=lambda candidate: (candidate[1], candidate[0], candidate[2], candidate[4])
        )
        return (serial, mod_id, model), 0.0

    def _run_detail_task(self, task: tuple[str, int, str]) -> None:
        serial, mod_id, model = task
        key = (serial, model)
        path = self._model_path(mod_id, model)
        started = time.monotonic()
        try:
            with self._detail_request_lock:
                payload = self._get_json(
                    path,
                    request_get=self.detail_request_get,
                    report_transport=False,
                    timeout=self.detail_request_timeout,
                )
            error = None
        except (requests.RequestException, ValueError, TypeError) as caught:
            payload = None
            error = caught
        now = self.clock()
        duration = time.monotonic() - started
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
                self._record_detail_failure(
                    key, error, now, stagger=True, duration=duration
                )
            else:
                self._record_detail_success(key, payload, now, duration=duration)

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
            state.update(self._decode_status("REbus_status", "fixed", rebus.get("St")))
            state.update({
                "rebus_power_w": _number(rebus, "P"),
                "accumulated_energy_kwh": (_number(rebus, "E") / 1000) if _number(rebus, "E") is not None else None,
                "voltage_v": _number(rebus, "V"),
                "current_a": _number(rebus, "I"),
                "temperature_c": _number(rebus, "T"),
                "event_code": rebus.get("Ev"),
                "rebus_bits": rebus.get("RB"),
            })

    def _apply_pv_directory(
        self, state: dict[str, Any], entry: dict[str, Any]
    ) -> bool:
        raw_status = entry.get("St")
        status_available = (
            isinstance(raw_status, (int, float))
            and not isinstance(raw_status, bool)
            and math.isfinite(raw_status)
            and int(raw_status) == raw_status
        )
        if status_available:
            state.update(self._decode_status("REbus_dir", "repeating", raw_status))
        power = _number(entry, "P")
        if _valid_pv_power(power):
            state["rebus_power_w"] = power
        else:
            state.pop("rebus_power_w", None)
        for target, source in (
            ("voltage_v", "V"),
            ("current_a", "I"),
            ("temperature_c", "T"),
        ):
            value = _number(entry, source)
            if value is not None:
                state[target] = value
        if "Rb" in entry:
            state["rebus_bits"] = entry["Rb"]
        if "UpdtTm" in entry:
            state["directory_updated_at"] = entry["UpdtTm"]
        return status_available

    def _decode_status(self, model, section, value):
        decoded = self.definitions.decode(model, section, "St", value)
        try:
            raw = int(value)
        except (TypeError, ValueError, OverflowError):
            raw = None
        return {
            "status": decoded.get("state") if decoded and decoded["available"] else None,
            "status_code": raw, "status_code_hex": f"0x{raw:04X}" if raw is not None else None,
            "status_severity": "error" if decoded and decoded["errors"] else "warning" if decoded and decoded["warnings"] else "normal" if decoded and decoded["available"] and not decoded.get("unknown_code") and decoded.get("symbol") != "UNKNOWN" else None,
        }

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
        directory_entry, directory_health = self._fresh_pv_link_directory_entry(
            serial, now
        )
        state["endpoint_health"]["controller_directory"] = directory_health
        directory_core_available = False
        if directory_entry is not None:
            directory_core_available = self._apply_pv_directory(
                state, directory_entry
            )
        directory_health["core_state_available"] = directory_core_available
        pvrss = self._fresh_fixed(state, "pvrss_telemetry")
        if pv:
            state.update({
                "input_voltage_v": _number(pv, "Vin"),
                "input_current_a": _number(pv, "Iin"),
                "maximum_current_a": _number(pv, "AMax"),
                "error_word": int(pv.get("ErrorWord", 0) or 0),
                "pv_status_word": pv.get("StatusWord"),
            })
        else:
            state["error_word"] = 0
        if directory_entry is not None:
            state["enabled"] = bool(
                self._directory_integer(directory_entry.get("Ena"))
            )
            state["enable_control_available"] = True
        else:
            state["enable_control_available"] = False
            if pv and "Ena" in pv:
                state["enabled"] = bool(pv["Ena"])
        if pvrss:
            state.update({
                "pvrss_status": pvrss.get("Status"),
                "pvrss_self_test": None,
                "snaprs_installed": _number(pvrss, "InstalledCount"),
                "snaprs_detected": _number(pvrss, "DetectedCount"),
                "number_of_strings": _number(pvrss, "NumStrings"),
                "telemetry_updated_at": pvrss.get("LastUpdatedUTCTimestamp"),
            })
        state["error_names"] = []
        if self.definitions:
            extra = ("REbus_dir", "repeating", directory_entry, directory_core_available) if directory_entry is not None else None
            self.definitions.enrich(state, extra)
            error_record = state["decoded_registers"].get("pvlink_status.fixed.ErrorWord")
            state["error_names"] = error_record["errors"] if error_record and error_record["available"] else []
            self_test = state["decoded_registers"].get("pvrss_telemetry.fixed.SelfTestResults")
            if self_test and self_test["available"]:
                state["pvrss_self_test"] = self_test["state"]
            if state.get("status_severity") is None:
                directory_core_available = False
            state["core_state_available"] = directory_core_available or bool(state.get("status"))
            directory_health["core_state_available"] = directory_core_available
        fault_reasons = []
        if state.get("status_severity") == "error":
            fault_reasons.append(state["status"])
        if self.definitions:
            fault_reasons.extend(state["active_errors"])
        assessment_models = {"REbus_status", "pvlink_status", "pvrss_telemetry"}
        assessment_complete = all(
            state["endpoint_health"][model]["fresh"] for model in assessment_models
        )
        detailed_fault_coverage = all(
            state["endpoint_health"][model]["fresh"]
            for model in ("pvlink_status", "pvrss_telemetry")
        )
        core_fault_available = directory_core_available or assessment_complete
        if self.definitions:
            decoded = state["decoded_registers"]
            detailed_fault_coverage = detailed_fault_coverage and all(
                (record := decoded.get(key)) and record["available"] and not record["unknown_mask"] and not record.get("unknown_code")
                for key in ("pvlink_status.fixed.ErrorWord", "pvrss_telemetry.fixed.Status", "pvrss_telemetry.fixed.SelfTestResults")
            )
            assessment_complete = assessment_complete and detailed_fault_coverage and state.get("status_severity") is not None
            core_fault_available = directory_core_available or assessment_complete
        state["fault_reasons"] = sorted(set(fault_reasons))
        state["fault_assessment_complete"] = assessment_complete
        state["detailed_fault_coverage"] = detailed_fault_coverage
        state["core_state_available"] = directory_core_available or (bool(state.get("status")) if self.definitions else bool(state["endpoint_health"]["REbus_status"].get("fresh")))
        state["fault"] = (
            True
            if state["fault_reasons"]
            else (False if core_fault_available else None)
        )
        state["fault_summary"] = (
            ", ".join(state["fault_reasons"])
            if state["fault_reasons"]
            else ("none" if core_fault_available else "unknown")
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
        if self.definitions:
            self.definitions.enrich(state)
        return state

    @staticmethod
    def _battery_module_indices(payload: Any) -> set[int]:
        if not isinstance(payload, dict):
            return set()
        indices: set[int] = set()
        count = _number(_fixed(payload), "NMod")
        if (
            isinstance(count, (int, float))
            and math.isfinite(count)
            and int(count) == count
            and 1 <= int(count) <= MAX_BATTERY_MODULES
        ):
            indices.update(range(1, int(count) + 1))
        repeating = payload.get("repeating")
        if isinstance(repeating, dict):
            for key in repeating:
                try:
                    index = int(key)
                except (TypeError, ValueError):
                    continue
                if str(index) == str(key) and 1 <= index <= MAX_BATTERY_MODULES:
                    indices.add(index)
        return indices

    @classmethod
    def _battery_module_group(cls, battery: dict[str, Any]) -> dict[str, Any]:
        serial = battery["serial"]
        health = dict(
            battery.get("endpoint_health", {}).get("lithium_ion_string", {})
        )
        group = {
            "battery_serial": serial,
            "endpoint_health": health,
            "modules": {},
        }
        if not health.get("fresh"):
            return group
        payload = battery.get("raw_models", {}).get("lithium_ion_string")
        if not isinstance(payload, dict):
            return group
        repeating = payload.get("repeating")
        repeating = repeating if isinstance(repeating, dict) else {}
        fields = {
            "state_of_charge_percent": "ModSoC",
            "state_of_health_percent": "ModSoH",
            "cell_count": "ModNCell",
            "minimum_cell_voltage_v": "ModCellVMin",
            "maximum_cell_voltage_v": "ModCellVMax",
            "average_cell_voltage_v": "ModCellVAvg",
            "minimum_cell_temperature_c": "ModCellTmpMin",
            "maximum_cell_temperature_c": "ModCellTmpMax",
            "average_cell_temperature_c": "ModCellTmpAvg",
        }
        for index in sorted(cls._battery_module_indices(payload)):
            raw = repeating.get(str(index))
            present = isinstance(raw, dict)
            module = {"index": index, "present": present}
            if present:
                for normalized, source in fields.items():
                    value = _number(raw, source)
                    if value is not None and math.isfinite(value):
                        module[normalized] = value
            group["modules"][str(index)] = module
        return group

    def _snapshot(self, now: float) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(now)

    def _snapshot_locked(self, now: float) -> dict[str, Any]:
        api_connected = bool(
            self._last_devices_success is not None
            and now - self._last_devices_success <= self.disconnect_after
        )
        if self.definitions:
            versions = {}
            for (serial, model), payload in self._details.items():
                if model == "common" and self._endpoint_health.get((serial, model), {}).get("last_success") is not None:
                    version = _fixed(payload).get("Vr")
                    if isinstance(version, str) and version:
                        versions[serial] = version
            self.definitions.note_versions(versions)
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
        batteries = []
        battery_modules = {}
        for serial, item in self._last_devices.items():
            if item["kind"] != "battery" or serial in self.ignored:
                continue
            battery = self._generic_state(serial, "battery", now)
            battery_modules[serial] = self._battery_module_group(battery)
            if self.definitions:
                all_decoded = battery.get("decoded_registers", {})
                for index, module in battery_modules[serial]["modules"].items():
                    raw_module = battery.get("raw_models", {}).get("lithium_ion_string", {}).get("repeating", {}).get(index, {})
                    module["decoded_registers"] = self.definitions.module_registers("lithium_ion_string", index, raw_module, module["present"])
                    module["serial"] = f"{serial}/module/{index}"
                battery["decoded_registers"] = {key: value for key, value in all_decoded.items() if not key.startswith("lithium_ion_string.repeating.")}
            battery.get("raw_models", {}).pop("lithium_ion_string", None)
            battery.get("endpoint_health", {}).pop("lithium_ion_string", None)
            if not battery.get("raw_models"):
                battery.pop("raw_models", None)
            batteries.append(battery)
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
        if self.definitions:
            system["register_definitions"] = self.definitions.health()
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
            "battery_modules": battery_modules,
            "pv_links": pvs,
            "untracked_pv_links": untracked,
        }
