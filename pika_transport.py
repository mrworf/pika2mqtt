"""Resilient SSH transport for the inverter's localhost installer API."""

from __future__ import annotations

import logging
import os
import random
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}=?$")


class TransportConfigurationError(ValueError):
    """The SSH transport cannot start with the supplied configuration."""


class HostKeyMismatch(RuntimeError):
    """The inverter presented a host key other than the configured key."""


class HostKeyUnavailable(RuntimeError):
    """The inverter host key could not be obtained during this attempt."""


@dataclass(frozen=True)
class SshTunnelConfig:
    hostname: str
    private_key: str
    host_fingerprint: str
    ssh_port: int = 22
    local_port: int = 18080
    remote_port: int = 80
    connect_timeout: int = 10
    server_alive_interval: int = 15
    server_alive_count_max: int = 3
    stable_after: float = 60.0
    max_backoff: float = 60.0
    failure_threshold: int = 3

    def validate(self) -> None:
        if not self.hostname:
            raise TransportConfigurationError("The inverter hostname is required")
        if not Path(self.private_key).is_file():
            raise TransportConfigurationError(
                f"SSH private key not found: {self.private_key}"
            )
        if not FINGERPRINT_RE.fullmatch(self.host_fingerprint or ""):
            raise TransportConfigurationError(
                "SSH host fingerprint must use the SHA256:<base64> format"
            )
        for name, value in (
            ("SSH port", self.ssh_port),
            ("local tunnel port", self.local_port),
            ("remote HTTP port", self.remote_port),
        ):
            if value < 1 or value > 65535:
                raise TransportConfigurationError(f"{name} is out of range: {value}")
        for executable in ("ssh", "ssh-keyscan", "ssh-keygen"):
            if shutil.which(executable) is None:
                raise TransportConfigurationError(
                    f"Required executable is not available: {executable}"
                )


class HostKeyVerifier:
    """Build an ephemeral known_hosts file after fingerprint verification."""

    def __init__(self, config: SshTunnelConfig, runtime_directory: str):
        self.config = config
        self.runtime_directory = runtime_directory

    def verify(self) -> str:
        command = [
            "ssh-keyscan",
            "-T",
            str(self.config.connect_timeout),
            "-p",
            str(self.config.ssh_port),
            "-t",
            "ed25519",
            self.config.hostname,
        ]
        try:
            scan = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.connect_timeout + 2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HostKeyUnavailable(str(error)) from error

        key_lines = [
            line for line in scan.stdout.splitlines() if line and not line.startswith("#")
        ]
        if scan.returncode != 0 or not key_lines:
            detail = scan.stderr.strip() or f"ssh-keyscan exited {scan.returncode}"
            raise HostKeyUnavailable(detail)

        observed = []
        verified_line = None
        for line in key_lines:
            try:
                fingerprint = subprocess.run(
                    ["ssh-keygen", "-lf", "-", "-E", "sha256"],
                    input=line + "\n",
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise HostKeyUnavailable(str(error)) from error
            if fingerprint.returncode != 0:
                continue
            fields = fingerprint.stdout.split()
            if len(fields) < 2:
                continue
            observed.append(fields[1])
            if fields[1] == self.config.host_fingerprint:
                verified_line = line
                break

        if verified_line is None:
            value = ", ".join(observed) if observed else "unreadable key"
            raise HostKeyMismatch(
                "Inverter SSH host-key mismatch: expected "
                f"{self.config.host_fingerprint}, observed {value}"
            )

        known_hosts = os.path.join(self.runtime_directory, "known_hosts")
        with open(known_hosts, "w", encoding="utf-8") as handle:
            handle.write(verified_line + "\n")
        os.chmod(known_hosts, 0o600)
        return known_hosts


class SshTunnelSupervisor:
    """Own one SSH tunnel and restore it after transient failures."""

    def __init__(self, config: SshTunnelConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self._available = threading.Event()
        self._stop = threading.Event()
        self._recycle = threading.Event()
        self._fatal = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None
        self._process_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = "disconnected"
        self._fatal_error: Optional[Exception] = None
        self._transport_failures = 0
        self._retry_attempt = 0
        self._runtime_directory: Optional[tempfile.TemporaryDirectory] = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.config.local_port}"

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def fatal_error(self) -> Optional[Exception]:
        return self._fatal_error

    def is_available(self) -> bool:
        return self._available.is_set()

    def wait_available(self, timeout: Optional[float] = None) -> bool:
        return self._available.wait(timeout)

    def wait_fatal(self, timeout: Optional[float] = None) -> bool:
        return self._fatal.wait(timeout)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.config.validate()
        self._runtime_directory = tempfile.TemporaryDirectory(prefix="pika2mqtt-ssh-")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="ssh-tunnel-supervisor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self.logger.info("Stopping SSH tunnel supervisor")
        self._set_state("stopping")
        self._stop.set()
        self._available.clear()
        self._recycle.set()
        self._terminate_process()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        if self._runtime_directory is not None:
            self._runtime_directory.cleanup()
            self._runtime_directory = None
        self.logger.info("SSH tunnel supervisor stopped")

    def report_success(self) -> None:
        with self._state_lock:
            self._transport_failures = 0
            self._retry_attempt = 0

    def report_transport_failure(self, error: BaseException) -> None:
        with self._state_lock:
            self._transport_failures += 1
            failures = self._transport_failures
        self.logger.warning(
            "Installer transport failure %d/%d: %s",
            failures,
            self.config.failure_threshold,
            error,
        )
        if failures >= self.config.failure_threshold:
            self.logger.warning("Recycling SSH tunnel after repeated transport failures")
            self._available.clear()
            self._recycle.set()
            self._terminate_process()

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self._state = state

    def _backoff_delay(self, attempt: int) -> float:
        base = min(self.config.max_backoff, float(2 ** max(0, attempt - 1)))
        return min(self.config.max_backoff, base * random.uniform(0.8, 1.2))

    def _build_command(self, known_hosts: str) -> list[str]:
        return [
            "ssh",
            "-N",
            "-i",
            self.config.private_key,
            "-p",
            str(self.config.ssh_port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            f"ConnectTimeout={self.config.connect_timeout}",
            "-o",
            f"ServerAliveInterval={self.config.server_alive_interval}",
            "-o",
            f"ServerAliveCountMax={self.config.server_alive_count_max}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-L",
            f"127.0.0.1:{self.config.local_port}:127.0.0.1:{self.config.remote_port}",
            f"root@{self.config.hostname}",
        ]

    def _run(self) -> None:
        outage_started = time.monotonic()
        while not self._stop.is_set():
            self._set_state("connecting")
            attempt = self._retry_attempt + 1
            self.logger.info(
                "Connecting SSH tunnel to %s:%d (attempt %d)",
                self.config.hostname,
                self.config.ssh_port,
                attempt,
            )
            try:
                verifier = HostKeyVerifier(
                    self.config, self._runtime_directory.name  # type: ignore[union-attr]
                )
                known_hosts = verifier.verify()
                command = self._build_command(known_hosts)
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                with self._process_lock:
                    self._process = process
                stderr_lines: list[str] = []
                stderr_thread = threading.Thread(
                    target=self._drain_stderr,
                    args=(process, stderr_lines),
                    name="ssh-tunnel-stderr",
                    daemon=True,
                )
                stderr_thread.start()

                if not self._wait_until_ready(process):
                    detail = stderr_lines[-1] if stderr_lines else "tunnel did not become ready"
                    raise HostKeyUnavailable(detail)

                connected_at = time.monotonic()
                self._recycle.clear()
                self._available.set()
                self._set_state("connected")
                downtime = connected_at - outage_started
                self.logger.info(
                    "SSH tunnel established after %.1f seconds of downtime", downtime
                )

                while not self._stop.is_set() and process.poll() is None:
                    if self._recycle.wait(0.5):
                        self._terminate_process()
                        break

                self._available.clear()
                if self._stop.is_set():
                    break
                uptime = time.monotonic() - connected_at
                if uptime >= self.config.stable_after:
                    self._retry_attempt = 0
                return_code = process.poll()
                detail = stderr_lines[-1] if stderr_lines else f"SSH exited {return_code}"
                self.logger.warning("SSH tunnel lost after %.1f seconds: %s", uptime, detail)
                outage_started = time.monotonic()
            except HostKeyMismatch as error:
                self._fatal_error = error
                self._set_state("fatal")
                self._fatal.set()
                self.logger.critical("%s", error)
                return
            except (HostKeyUnavailable, OSError) as error:
                self._available.clear()
                self.logger.warning("SSH tunnel connection failed: %s", error)
            finally:
                self._available.clear()
                self._clear_process()

            if self._stop.is_set():
                break
            self._retry_attempt += 1
            delay = self._backoff_delay(self._retry_attempt)
            self._set_state("disconnected")
            self.logger.info(
                "SSH tunnel reconnect scheduled in %.1f seconds (attempt %d)",
                delay,
                self._retry_attempt + 1,
            )
            self._stop.wait(delay)

        self._set_state("stopping")

    def _wait_until_ready(self, process: subprocess.Popen) -> bool:
        deadline = time.monotonic() + self.config.connect_timeout
        while not self._stop.is_set() and time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            try:
                with socket.create_connection(
                    ("127.0.0.1", self.config.local_port), timeout=0.5
                ):
                    return True
            except OSError:
                self._stop.wait(0.1)
        return False

    def _drain_stderr(
        self, process: subprocess.Popen, captured_lines: list[str]
    ) -> None:
        if process.stderr is None:
            return
        for raw_line in process.stderr:
            line = raw_line.strip()
            if not line:
                continue
            captured_lines.append(line)
            if len(captured_lines) > 20:
                del captured_lines[:-20]
            self.logger.debug("SSH: %s", line)

    def _terminate_process(self) -> None:
        with self._process_lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _clear_process(self) -> None:
        with self._process_lock:
            process = self._process
            self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
