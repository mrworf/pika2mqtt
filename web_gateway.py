"""Authenticated HTTP gateway for the installer site behind the SSH tunnel."""

from __future__ import annotations

import base64
import binascii
import hmac
import http.client
import http.server
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit


SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))
HOP_BY_HOP_HEADERS = frozenset(
    (
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    )
)


class GatewayConfigurationError(ValueError):
    """The web gateway configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class WebGatewayConfig:
    enabled: bool = False
    allow_writes: bool = False
    listen_address: str = "0.0.0.0"
    listen_port: int = 8000
    username: Optional[str] = None
    password: Optional[str] = None
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 18080
    upstream_timeout: float = 10.0

    def validate(self) -> None:
        if self.allow_writes and not self.enabled:
            raise GatewayConfigurationError(
                "WEB_WRITE_ENABLED requires WEB_ENABLED"
            )
        if not self.enabled:
            return
        if not self.username:
            raise GatewayConfigurationError(
                "WEB_USERNAME is required when the web gateway is enabled"
            )
        if self.password is None or self.password == "":
            raise GatewayConfigurationError(
                "WEB_PASSWORD_FILE or WEB_PASSWORD is required when the web gateway is enabled"
            )
        if self.listen_port < 0 or self.listen_port > 65535:
            raise GatewayConfigurationError(
                f"Web gateway port is out of range: {self.listen_port}"
            )


def resolve_web_password(
    password_file: Optional[str], password_value: Optional[str]
) -> Optional[str]:
    """Resolve a gateway password, preferring a mounted secret file."""

    if password_file:
        path = Path(password_file)
        try:
            return path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as error:
            raise GatewayConfigurationError(
                f"Unable to read WEB_PASSWORD_FILE {password_file}: {error}"
            ) from error
    return password_value


class _GatewayServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class InstallerWebGateway:
    def __init__(self, config: WebGatewayConfig, transport, logger=None):
        self.config = config
        self.transport = transport
        self.logger = logger or logging.getLogger(__name__)
        self._server: Optional[_GatewayServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def bound_port(self) -> Optional[int]:
        if self._server is None:
            return None
        return self._server.server_address[1]

    def start(self) -> None:
        self.config.validate()
        if not self.config.enabled:
            self.logger.info("Installer web gateway is disabled")
            return
        handler = self._handler_class()
        try:
            self._server = _GatewayServer(
                (self.config.listen_address, self.config.listen_port), handler
            )
        except OSError as error:
            raise GatewayConfigurationError(
                "Unable to bind installer web gateway to "
                f"{self.config.listen_address}:{self.config.listen_port}: {error}"
            ) from error
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="installer-web-gateway",
            daemon=True,
        )
        self._thread.start()
        mode = "write-enabled" if self.config.allow_writes else "read-only"
        self.logger.info(
            "Installer web gateway listening on %s:%d (%s)",
            self.config.listen_address,
            self.bound_port,
            mode,
        )

    def stop(self) -> None:
        if self._server is None:
            return
        self.logger.info("Stopping installer web gateway")
        self._server.shutdown()
        self._server.server_close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        self.logger.info("Installer web gateway stopped")

    def _handler_class(self):
        gateway = self

        class GatewayRequestHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def __getattr__(self, name):
                if name.startswith("do_"):
                    method = name[3:]
                    return lambda: self._proxy(method)
                raise AttributeError(name)

            def _proxy(self, method: str) -> None:
                if not self._authenticate():
                    return
                if not gateway.config.allow_writes and method not in SAFE_METHODS:
                    self._error(
                        405,
                        "Write methods are disabled by the gateway",
                        extra_headers={"Allow": ", ".join(sorted(SAFE_METHODS))},
                    )
                    return
                if not self.path.startswith("/"):
                    self._error(400, "Only origin-form request paths are accepted")
                    return
                parsed = urlsplit(self.path)
                if parsed.scheme or parsed.netloc:
                    self._error(400, "Absolute request URLs are not accepted")
                    return
                if not gateway.transport.is_available():
                    self._error(503, "Installer tunnel is unavailable")
                    return

                try:
                    body = self._read_request_body()
                    headers = self._upstream_headers(body)
                    connection = http.client.HTTPConnection(
                        gateway.config.upstream_host,
                        gateway.config.upstream_port,
                        timeout=gateway.config.upstream_timeout,
                    )
                    connection.request(method, self.path, body=body, headers=headers)
                    response = connection.getresponse()
                except ValueError as error:
                    gateway.logger.debug("Invalid gateway request body: %s", error)
                    self._error(400, "Invalid request body")
                    return
                except (OSError, http.client.HTTPException) as error:
                    gateway.transport.report_transport_failure(error)
                    gateway.logger.warning(
                        "Installer gateway upstream failure for %s %s: %s",
                        method,
                        self.path,
                        error,
                    )
                    self._error(502, "Installer request failed")
                    return

                gateway.transport.report_success()
                try:
                    self.send_response_only(response.status, response.reason)
                    has_length = False
                    for name, value in response.getheaders():
                        lower_name = name.lower()
                        if lower_name in HOP_BY_HOP_HEADERS:
                            continue
                        if lower_name == "content-length":
                            has_length = True
                        self.send_header(name, value)
                    if not has_length:
                        self.send_header("Connection", "close")
                        self.close_connection = True
                    self.end_headers()
                    if method != "HEAD":
                        while True:
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    gateway.logger.debug(
                        "Gateway client disconnected during %s %s", method, self.path
                    )
                except (OSError, http.client.HTTPException) as error:
                    gateway.transport.report_transport_failure(error)
                    gateway.logger.warning(
                        "Installer gateway response interrupted for %s %s: %s",
                        method,
                        self.path,
                        error,
                    )
                finally:
                    response.close()
                    connection.close()

            def _authenticate(self) -> bool:
                value = self.headers.get("Authorization", "")
                if not value.startswith("Basic "):
                    self._authentication_required()
                    return False
                try:
                    decoded = base64.b64decode(value[6:], validate=True).decode("utf-8")
                    username, password = decoded.split(":", 1)
                except (binascii.Error, UnicodeDecodeError, ValueError):
                    self._authentication_required()
                    return False
                valid_user = hmac.compare_digest(username, gateway.config.username or "")
                valid_password = hmac.compare_digest(
                    password, gateway.config.password or ""
                )
                if not (valid_user and valid_password):
                    self._authentication_required()
                    return False
                return True

            def _authentication_required(self) -> None:
                self._error(
                    401,
                    "Authentication required",
                    extra_headers={"WWW-Authenticate": 'Basic realm="pika2mqtt"'},
                )

            def _read_request_body(self) -> Optional[bytes]:
                transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
                if transfer_encoding == "chunked":
                    chunks = []
                    while True:
                        size_line = self.rfile.readline().split(b";", 1)[0].strip()
                        size = int(size_line, 16)
                        if size == 0:
                            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                                pass
                            break
                        chunks.append(self.rfile.read(size))
                        self.rfile.read(2)
                    return b"".join(chunks)
                content_length = self.headers.get("Content-Length")
                if content_length is None:
                    return None
                return self.rfile.read(int(content_length))

            def _upstream_headers(self, body: Optional[bytes]) -> dict[str, str]:
                headers = {}
                connection_tokens = {
                    token.strip().lower()
                    for token in self.headers.get("Connection", "").split(",")
                    if token.strip()
                }
                for name, value in self.headers.items():
                    lower_name = name.lower()
                    if (
                        lower_name in HOP_BY_HOP_HEADERS
                        or lower_name in connection_tokens
                        or lower_name in ("authorization", "host", "content-length")
                    ):
                        continue
                    headers[name] = value
                headers["Host"] = (
                    f"{gateway.config.upstream_host}:{gateway.config.upstream_port}"
                )
                if body is not None:
                    headers["Content-Length"] = str(len(body))
                return headers

            def _error(
                self, status: int, message: str, extra_headers: Optional[dict] = None
            ) -> None:
                body = (message + "\n").encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.close_connection = True
                if extra_headers:
                    for name, value in extra_headers.items():
                        self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def log_message(self, format_string, *args):
                gateway.logger.debug(
                    "Gateway client %s: %s", self.client_address[0], format_string % args
                )

        return GatewayRequestHandler
