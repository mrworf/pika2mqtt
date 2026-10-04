"""Firmware-owned SunSpec interpretation, reference rendering and observations.

No numeric enum or bit mappings live here. XML supplies the wire definitions;
the separately editable policy supplies application severity, never wire values.
"""
from __future__ import annotations

import copy
import hashlib
import html
import io
import json
import logging
import os
import random
import tarfile
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

LOG = logging.getLogger(__name__)
SOURCE = "/opt/pika/sunspec-models/smdx"
MAX_ARCHIVE = 16 * 1024 * 1024


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as handle:
            name = handle.name
            handle.write(content)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def load_policy(path=None):
    try:
        policy = json.loads(Path(path or Path(__file__).with_name("register_policy.json")).read_text())
        if not isinstance(policy, dict) or set(policy) != {"version", "error_registers", "bitfields", "assessment_registers", "errors", "warnings"} or isinstance(policy["version"], bool) or policy["version"] != 1:
            raise ValueError("expected policy version 1 and all six policy fields")
        for key in ("error_registers", "bitfields"):
            if not isinstance(policy[key], list) or any(not isinstance(v, str) or "." not in v for v in policy[key]):
                raise ValueError(f"{key} must contain model/register names")
        for key in ("errors", "warnings", "assessment_registers"):
            if not isinstance(policy[key], dict):
                raise ValueError(f"{key} must be an object")
            for register, symbols in policy[key].items():
                if not isinstance(register, str) or (key != "assessment_registers" and "." not in register) or not isinstance(symbols, list) or any(not isinstance(v, str) or not v or (key == "assessment_registers" and "." not in v) for v in symbols):
                    raise ValueError(f"invalid {key} symbol list")
        return policy
    except (OSError, ValueError, TypeError) as error:
        raise ValueError(f"Invalid register policy {path or 'bundled default'}: {error}") from error


def reserved(symbol):
    return symbol.upper().startswith(("UNUSED", "RESERVED"))


def integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return int(value) if int(value) == value and value >= 0 else None
    except (ValueError, OverflowError):
        return None


class DefinitionSet:
    def __init__(self, files, policy):
        self.policy = policy
        self.registers = {}
        self.issues = []
        digest = hashlib.sha256()
        for filename, content in sorted(files.items()):
            if len(content) > 1024 * 1024 or b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                raise ValueError(f"unsafe or oversized XML: {filename}")
            digest.update(filename.encode() + b"\0" + content)
            try:
                root = ET.fromstring(content)
            except ET.ParseError as error:
                raise ValueError(f"malformed XML {filename}: {error}") from error
            for model in root.findall("model"):
                model_name = model.get("name")
                if not model_name:
                    continue
                strings = root.find(f"strings[@id='{model.get('id')}'][@locale='en']")
                for block in model.findall("block"):
                    section = "repeating" if block.get("type") == "repeating" else "fixed"
                    for point in block.findall("point"):
                        symbols = point.findall("symbol")
                        if not symbols:
                            continue
                        field = point.get("id")
                        key = (model_name, section, field)
                        kind = point.get("type", "")
                        if f"{model_name}.{section}.{field}" in policy["bitfields"]:
                            kind = "bitfield16"
                        if kind not in ("enum16", "enum32", "bitfield16", "bitfield32"):
                            continue
                        metadata = strings.find(f"point[@id='{field}']") if strings is not None else None
                        description = (metadata.findtext("description") or "").strip() if metadata is not None else ""
                        label = (metadata.findtext("label") or field).strip() if metadata is not None else field
                        mapping = {}
                        ambiguous = False
                        for symbol in symbols:
                            try:
                                value = int(symbol.text.strip(), 0)
                            except (ValueError, AttributeError):
                                ambiguous = True
                                continue
                            if value < 0 or value >= (16 if kind == "bitfield16" else 32 if kind == "bitfield32" else 2 ** (16 if kind == "enum16" else 32)):
                                ambiguous = True
                            if value in mapping:
                                ambiguous = True
                            sym_id = symbol.get("id")
                            if not sym_id:
                                ambiguous = True
                                continue
                            sym_meta = metadata.find(f"symbol[@id='{sym_id}']") if metadata is not None else None
                            mapping[value] = {"symbol": sym_id, "label": ((sym_meta.findtext("label") or sym_id.replace("_", " ").title()).strip() if sym_meta is not None else sym_id.replace("_", " ").title()), "description": ((sym_meta.findtext("description") or "").strip() if sym_meta is not None else "")}
                        definition = {"kind": kind, "label": label, "description": description,
                                      "symbols": mapping, "ambiguous": ambiguous}
                        previous = self.registers.get(key)
                        signature = lambda d: (d["kind"], {v: s["symbol"] for v, s in d["symbols"].items()}, d["ambiguous"])
                        if previous is not None and signature(previous) != signature(definition):
                            definition["ambiguous"] = True
                            self.issues.append(f"conflicting definitions for {model_name}.{section}.{field}")
                        if ambiguous:
                            self.issues.append(f"ambiguous symbols for {model_name}.{section}.{field}")
                        self.registers[key] = definition
        if not self.registers:
            raise ValueError("no supported register definitions found")
        self.checksum = digest.hexdigest()

    def severity(self, model, field, symbol):
        key = f"{model}.{field}"
        if field == "Ev":
            return "info"
        if key in self.policy["error_registers"] and not reserved(symbol):
            return "error"
        if symbol in self.policy["errors"].get(key, []):
            return "error"
        if symbol in self.policy["warnings"].get(key, []):
            return "warning"
        return "info"

    def decode(self, model, section, field, value, fresh=True):
        definition = self.registers.get((model, section, field))
        if not definition:
            return None
        result = {"model": model, "section": section, "register": field,
                  **copy.deepcopy(definition), "raw": value, "available": False,
                  "symbol": None, "state": None, "active_symbols": [], "errors": [], "warnings": [],
                  "unknown_mask": 0, "checksum": self.checksum}
        # JSON object keys are strings. Keep the definition mapping internal.
        result.pop("symbols")
        result["supported_states"] = [v["symbol"].lower() for _, v in sorted(definition["symbols"].items())]
        result["flags"] = {str(bit): {**meta, "severity": self.severity(model, field, meta["symbol"])}
                           for bit, meta in definition["symbols"].items() if not reserved(meta["symbol"])} if definition["kind"].startswith("bitfield") else {}
        raw = integer(value)
        if not fresh or definition["ambiguous"] or raw is None:
            return result
        result["available"] = True
        if definition["kind"].startswith("enum"):
            meta = definition["symbols"].get(raw)
            result["symbol"] = meta["symbol"] if meta else None
            result["description"] = (meta["description"] or definition["description"]) if meta else ""
            result["state"] = meta["symbol"].lower() if meta else f"unknown_0x{raw:X}"
            active = [meta["symbol"]] if meta else []
            result["unknown_code"] = meta is None
        else:
            known_mask = sum(1 << bit for bit in definition["symbols"])
            result["unknown_mask"] = raw & ~known_mask
            active = [meta["symbol"] for bit, meta in sorted(definition["symbols"].items()) if raw & (1 << bit) and not reserved(meta["symbol"])]
        result["active_symbols"] = active
        result["errors"] = [s for s in active if self.severity(model, field, s) == "error"]
        result["warnings"] = [s for s in active if self.severity(model, field, s) == "warning"]
        return result

    def decode_payload(self, model, payload, fresh):
        result = {}
        fixed = payload.get("fixed", {})
        repeats = payload.get("repeating", {})
        repeats = repeats if isinstance(repeats, dict) else {}
        for (name, section, field), definition in sorted(self.registers.items()):
            if name != model:
                continue
            records = {"": fixed} if section == "fixed" else repeats
            for index, record in records.items():
                if not isinstance(record, dict):
                    continue
                key = f"{model}.{section}.{str(index) + '.' if index else ''}{field}"
                result[key] = self.decode(model, section, field, record.get(field), fresh)
        return result

    def repeat_record(self, model, index, record, fresh):
        return self.decode_payload(model, {"repeating": {str(index): record}}, fresh)

    def reference(self, versions):
        def cell(value):
            return str(value).replace("|", "\\|").replace("\n", " ")
        lines = ["# Firmware register reference", "", f"Source: `{SOURCE}`", "",
                 f"Definition SHA256: `{self.checksum}`", "",
                 f"Reported firmware: {', '.join(f'{s}: {v}' for s, v in sorted(versions.items())) or 'not yet reported'}", "",
                 "Enums describe one state; bitfields describe simultaneous flags. Ev is the last event, not an active fault or event history.", "",
                 "Home Assistant read-only flags are sensors displaying True when the named firmware bit is active and False when inactive. Unknown or unavailable is not False. Automation states are the strings `True` and `False`, not on/off.", "",
                 "Severity is application policy, not firmware documentation. RESERVED/UNUSED symbols are raw diagnostics only. Missing descriptions are not inferred.", ""]
        for (model, section, field), definition in sorted(self.registers.items()):
            lines += [f"## {model}.{section}.{field}", "", cell(definition["description"] or "No description provided by firmware."), "",
                      f"Type: {definition['kind']}. Interpretation: {'unavailable (ambiguous definition)' if definition['ambiguous'] else 'valid'}.", "",
                      "| Symbol | Bit position / enum value | Policy classification | Firmware description |", "|---|---:|---|---|"]
            for value, metadata in sorted(definition["symbols"].items()):
                symbol = metadata["symbol"]
                lines.append(f"| {cell(symbol)} | {value} | {'reserved' if reserved(symbol) else self.severity(model, field, symbol)} | {cell(metadata['description'] or 'No description provided by firmware.')} |")
            lines.append("")
        return "\n".join(lines)

    def html_reference(self, versions):
        escape = lambda value: html.escape(str(value))
        parts = ['<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Firmware register reference</title><style>body{max-width:1100px;margin:2em auto;padding:0 1em;font:16px system-ui}table{border-collapse:collapse;width:100%;margin:1em 0}th,td{border:1px solid #aaa;padding:.5em;text-align:left;overflow-wrap:anywhere}code{overflow-wrap:anywhere}section{margin:2em 0}</style></head><body><h1>Firmware register reference</h1><p><a href="/diagnostics/registers.md">Download Markdown</a></p>',
                 f'<p>Source: <code>{escape(SOURCE)}</code><br>Definition SHA256: <code>{escape(self.checksum)}</code></p>',
                 f'<p>Reported firmware: {escape(versions or "not yet reported")}</p>',
                 '<p>Enums describe one state; bitfields describe simultaneous flags. Ev is the last event, not an active fault or event history. Classification is application policy, not firmware documentation.</p>',
                 '<p>Home Assistant read-only flags are sensors displaying True when the named firmware bit is active and False when inactive. Unknown or unavailable is not False. Automation states are the strings <code>True</code> and <code>False</code>, not on/off.</p>']
        for (model, section, field), definition in sorted(self.registers.items()):
            parts += [f'<section><h2>{escape(model)}.{escape(section)}.{escape(field)}</h2>',
                      f'<p>{escape(definition["description"] or "No description provided by firmware.")}</p>',
                      f'<p>Type: {escape(definition["kind"])}. Interpretation: {"unavailable (ambiguous definition)" if definition["ambiguous"] else "valid"}.</p>',
                      '<table><thead><tr><th>Symbol</th><th>Bit position / enum value</th><th>Policy classification</th><th>Firmware description</th></tr></thead><tbody>']
            for value, meta in sorted(definition["symbols"].items()):
                symbol = meta["symbol"]
                parts.append(f'<tr><td>{escape(symbol)}</td><td>{value}</td><td>{escape("reserved" if reserved(symbol) else self.severity(model, field, symbol))}</td><td>{escape(meta["description"] or "No description provided by firmware.")}</td></tr>')
            parts.append('</tbody></table></section>')
        return ''.join(parts) + '</body></html>'


class RegisterDefinitions:
    """Independent SSH loader and shared immutable decoding snapshot."""
    def __init__(self, transport, policy_path=None, data_directory="/data"):
        self.transport = transport
        self.policy = load_policy(policy_path)
        self.data_directory = Path(data_directory)
        self._lock = threading.RLock()
        self._definitions = None
        self._valid = False
        self._versions = {}
        self._revision = 0
        self._loaded_revision = -1
        self._loaded_generation = -1
        self._reference = None
        self._last_error = None
        self._observations = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="firmware-definitions", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=35)

    def note_versions(self, versions):
        with self._lock:
            changed = any(serial in self._versions and self._versions[serial] != version for serial, version in versions.items())
            added = any(serial not in self._versions for serial in versions)
            self._versions.update(versions)
            if changed:
                self._valid = False
                self._revision += 1
                self._reference = None
                LOG.warning("Firmware version changed; refreshing register definitions")
                self._wake.set()
            elif added and self._definitions:
                self._reference = self._definitions.reference(self._versions)
                self._wake.set()

    def health(self):
        with self._lock:
            return {"available": self._valid, "checksum": self._definitions.checksum if self._definitions else None,
                    "source": SOURCE, "error": self._last_error, "firmware_versions": dict(self._versions)}

    def decode(self, *args, **kwargs):
        with self._lock:
            return self._definitions.decode(*args, **kwargs) if self._valid else None

    def module_registers(self, model, index, record, fresh):
        with self._lock:
            return self._definitions.repeat_record(model, index, record, fresh and self._valid) if self._definitions else {}

    def reference(self, html_format=False):
        with self._lock:
            reference = self._reference if self._valid else None
            definitions = self._definitions
            versions = dict(self._versions)
        if reference is None:
            return None
        if not html_format:
            return reference
        return definitions.html_reference(versions)

    def install(self, files):
        definitions = DefinitionSet(files, self.policy)
        with self._lock:
            self._definitions = definitions
            self._valid = True
            self._last_error = None
            self._reference = definitions.reference(self._versions)
        LOG.info("Loaded firmware register definitions from %s SHA256=%s", SOURCE, definitions.checksum)
        for issue in definitions.issues:
            LOG.warning("Firmware definition: %s", issue)

    @staticmethod
    def archive_files(data):
        if len(data) > MAX_ARCHIVE:
            raise ValueError("definition archive is oversized")
        files = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for member in archive:
                if not member.isfile() or not member.name.endswith(".xml"):
                    continue
                if member.size > 1024 * 1024 or len(files) >= 512:
                    raise ValueError("definition archive exceeds limits")
                files[member.name] = archive.extractfile(member).read()
        return files

    def _run(self):
        attempt = 0
        next_attempt = 0.0
        last_written = None
        next_write_attempt = 0.0
        last_write_error = None
        while not self._stop.is_set():
            generation = self.transport.connection_generation
            with self._lock:
                revision = self._revision
                needed = self._loaded_generation != generation or self._loaded_revision != revision or not self._valid
            if needed and self.transport.is_available() and time.monotonic() >= next_attempt:
                try:
                    files = self.archive_files(self.transport.read_definition_archive())
                    with self._lock:
                        if revision != self._revision or generation != self.transport.connection_generation:
                            continue
                        self.install(files)
                        self._loaded_generation = generation
                        self._loaded_revision = revision
                    attempt = 0
                    next_attempt = 0
                except (OSError, ValueError, tarfile.TarError) as error:
                    message = str(error)
                    attempt += 1
                    delay = min(60.0, 2 ** min(attempt, 6) * random.uniform(0.8, 1.2))
                    with self._lock:
                        previous = self._last_error
                        self._last_error = message
                    (LOG.warning if previous != message else LOG.debug)("Register definitions unavailable; retrying in %.1fs: %s", delay, message)
                    next_attempt = time.monotonic() + delay
            reference = self.reference()
            if reference and reference != last_written and time.monotonic() >= next_write_attempt:
                try:
                    atomic_write(self.data_directory / "register_reference.md", reference)
                    last_written = reference
                    last_write_error = None
                except OSError as error:
                    message = str(error)
                    (LOG.warning if message != last_write_error else LOG.debug)("Cannot write register reference; retrying in 60s: %s", message)
                    last_write_error = message
                    next_write_attempt = time.monotonic() + 60
            self._wake.wait(1)
            self._wake.clear()

    def enrich(self, state, extra=None):
        with self._lock:
            definitions = self._definitions
            valid = self._valid
        decoded = {}
        if definitions:
            models = set(state.get("raw_models", {})) | set(state.get("endpoint_health", {}))
            for model in sorted(models):
                payload = state.get("raw_models", {}).get(model, {})
                if not isinstance(payload, dict):
                    continue
                fresh = valid and state.get("connected", True) and state.get("endpoint_health", {}).get(model, {}).get("fresh", False)
                decoded.update(definitions.decode_payload(model, payload, fresh))
            if extra:
                model, section, record, fresh = extra
                for name, sec, field in definitions.registers:
                    if name == model and sec == section:
                        decoded[f"{model}.{section}.{field}"] = definitions.decode(model, section, field, record.get(field), valid and fresh)
        state["decoded_registers"] = decoded
        errors = sorted({f"{r['model']}.{r['register']}:{symbol}" for r in decoded.values() if r["available"] for symbol in r["errors"]})
        required = self.policy["assessment_registers"].get(state.get("kind"), [])
        coverage = valid and bool(required) and all(any(f"{r['model']}.{r['register']}" == key and r["available"] and not r["unknown_mask"] and not r.get("unknown_code") and r.get("symbol") != "UNKNOWN" for r in decoded.values()) for key in required)
        state["active_errors"] = errors
        state["active_error_count"] = len(errors) if coverage else None
        state["error_coverage_complete"] = coverage
        rebus = decoded.get("REbus_status.fixed.Ev")
        state["last_event"] = rebus.get("state") if rebus and rebus["available"] else None
        state["last_event_attributes"] = {k: rebus.get(k) for k in ("raw", "symbol", "description", "checksum")} if rebus else {}
        for key, record in decoded.items():
            self._observe(state.get("serial", "unknown"), key, record)

    def _observe(self, serial, key, record):
        identity = (serial, key)
        previous = self._observations.get(identity)
        if not record["available"]:
            if previous:
                previous["gap"] = True
            return
        signature = (record["raw"], record["checksum"])
        gap = bool(previous and previous.get("gap"))
        if previous and previous["signature"] == signature and not gap:
            return
        symbols = set(record["active_symbols"])
        errors = set(record["errors"])
        baseline = previous is None or gap or previous["signature"][1] != record["checksum"]
        label = "initial observation" if previous is None else "recovered observation" if gap else "changed"
        log = LOG.warning if errors or record["warnings"] or record["unknown_mask"] or record.get("unknown_code") else LOG.info
        log("%s %s %s: %s (raw=%s)", serial, key, label, ', '.join(sorted(symbols)) or record.get("state") or "none", record["raw"])
        unknown = (record["raw"] if record.get("unknown_code") else None, record["unknown_mask"], record["checksum"])
        if (record["unknown_mask"] or record.get("unknown_code")) and (not previous or previous.get("unknown") != unknown):
            LOG.warning("%s %s unknown indicator: code=%s mask=0x%X", serial, key, record["raw"], record["unknown_mask"])
        if not baseline:
            for symbol in sorted(errors - previous["errors"]):
                LOG.warning("%s %s error activated: %s", serial, key, symbol)
            for symbol in sorted(previous["errors"] - errors):
                LOG.info("%s %s error cleared: %s", serial, key, symbol)
            for symbol in sorted(symbols ^ previous["symbols"]):
                if symbol not in errors | previous["errors"]:
                    LOG.info("%s %s flag %s: %s", serial, key, "activated" if symbol in symbols else "cleared", symbol)
        LOG.debug("%s %s decoded register: %s", serial, key, record)
        self._observations[identity] = {"signature": signature, "symbols": symbols, "errors": errors, "gap": False, "unknown": unknown}
