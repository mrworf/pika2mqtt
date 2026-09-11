import base64
import http.client
import http.server
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from web_gateway import (
    GatewayConfigurationError,
    InstallerWebGateway,
    WebGatewayConfig,
    resolve_web_password,
)


class FakeTransport:
    def __init__(self, available=True):
        self.available = available
        self.successes = 0
        self.failures = []

    def is_available(self):
        return self.available

    def report_success(self):
        self.successes += 1

    def report_transport_failure(self, error):
        self.failures.append(error)


class FakeInstallerHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = []

    def _respond(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.__class__.requests.append(
            (self.command, self.path, dict(self.headers.items()), body)
        )
        if self.path.startswith("/slow"):
            time.sleep(0.3)
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        response = body or f"{self.command} {self.path}".encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/x-test")
        self.send_header("X-Upstream", "preserved")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response)

    do_GET = _respond
    do_HEAD = _respond
    do_OPTIONS = _respond
    do_POST = _respond
    do_PATCH = _respond

    def log_message(self, format_string, *args):
        pass


class GatewayTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeInstallerHandler
        )
        cls.upstream_thread = threading.Thread(
            target=cls.upstream.serve_forever, daemon=True
        )
        cls.upstream_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.upstream_thread.join(timeout=2)

    def setUp(self):
        FakeInstallerHandler.requests = []
        self.transport = FakeTransport()
        self.gateways = []

    def tearDown(self):
        for gateway in self.gateways:
            gateway.stop()

    def start_gateway(self, allow_writes=False, transport=None, upstream_port=None):
        config = WebGatewayConfig(
            enabled=True,
            allow_writes=allow_writes,
            listen_address="127.0.0.1",
            listen_port=0,
            username="operator",
            password="secret",
            upstream_port=upstream_port or self.upstream.server_address[1],
            upstream_timeout=1,
        )
        gateway = InstallerWebGateway(config, transport or self.transport)
        gateway.start()
        self.gateways.append(gateway)
        return gateway

    def authorization(self, username="operator", password="secret"):
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def request(self, gateway, method="GET", path="/", body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", gateway.bound_port, timeout=2
        )
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def test_authentication_is_always_required(self):
        gateway = self.start_gateway()
        status, headers, _ = self.request(gateway)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])

        status, _, _ = self.request(
            gateway, headers=self.authorization(password="wrong")
        )
        self.assertEqual(status, 401)
        self.assertEqual(FakeInstallerHandler.requests, [])

    def test_read_only_gateway_preserves_response_and_query(self):
        gateway = self.start_gateway()
        status, headers, body = self.request(
            gateway,
            path="/devices?fresh=true",
            headers={**self.authorization(), "adminToken": "installer-token"},
        )

        self.assertEqual(status, 201)
        self.assertEqual(headers["Content-Type"], "application/x-test")
        self.assertEqual(headers["X-Upstream"], "preserved")
        self.assertEqual(body, b"GET /devices?fresh=true")
        method, path, upstream_headers, _ = FakeInstallerHandler.requests[-1]
        self.assertEqual((method, path), ("GET", "/devices?fresh=true"))
        self.assertEqual(upstream_headers["adminToken"], "installer-token")
        self.assertNotIn("Authorization", upstream_headers)
        self.assertEqual(self.transport.successes, 1)

    def test_head_options_and_redirect_headers_are_preserved(self):
        gateway = self.start_gateway()
        for method in ("HEAD", "OPTIONS"):
            status, headers, body = self.request(
                gateway, method=method, path="/devices", headers=self.authorization()
            )
            self.assertEqual(status, 201)
            self.assertEqual(headers["X-Upstream"], "preserved")
            if method == "HEAD":
                self.assertEqual(body, b"")

        status, headers, _ = self.request(
            gateway, path="/redirect", headers=self.authorization()
        )
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/target")

    def test_read_only_gateway_denies_post(self):
        gateway = self.start_gateway()
        status, headers, _ = self.request(
            gateway,
            method="POST",
            path="/device/9/model/inverter_status",
            body=b"SysMd=3",
            headers=self.authorization(),
        )
        self.assertEqual(status, 405)
        self.assertIn("GET", headers["Allow"])
        self.assertEqual(FakeInstallerHandler.requests, [])

    def test_write_mode_forwards_post_and_arbitrary_method_body(self):
        gateway = self.start_gateway(allow_writes=True)
        for method in ("POST", "PATCH"):
            status, _, body = self.request(
                gateway,
                method=method,
                path="/device/9/model/inverter_status",
                body=b"SysMd=3",
                headers={**self.authorization(), "Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(status, 201)
            self.assertEqual(body, b"SysMd=3")
        self.assertEqual(
            [entry[0] for entry in FakeInstallerHandler.requests], ["POST", "PATCH"]
        )

    def test_disconnected_tunnel_returns_503(self):
        transport = FakeTransport(available=False)
        gateway = self.start_gateway(transport=transport)
        status, _, _ = self.request(gateway, headers=self.authorization())
        self.assertEqual(status, 503)

    def test_upstream_failure_returns_502_and_reports_transport_failure(self):
        gateway = self.start_gateway(upstream_port=1)
        status, _, _ = self.request(gateway, headers=self.authorization())
        self.assertEqual(status, 502)
        self.assertEqual(len(self.transport.failures), 1)

    def test_threaded_gateway_does_not_serialize_browser_requests(self):
        gateway = self.start_gateway()
        results = {}

        def get(name, path):
            started = time.monotonic()
            results[name] = (
                self.request(gateway, path=path, headers=self.authorization()),
                time.monotonic() - started,
            )

        slow = threading.Thread(target=get, args=("slow", "/slow"))
        fast = threading.Thread(target=get, args=("fast", "/fast"))
        slow.start()
        time.sleep(0.05)
        fast.start()
        slow.join()
        fast.join()

        self.assertEqual(results["fast"][0][0], 201)
        self.assertLess(results["fast"][1], results["slow"][1])


class GatewayConfigurationTests(unittest.TestCase):
    def test_gateway_is_disabled_without_creating_listener(self):
        gateway = InstallerWebGateway(WebGatewayConfig(), FakeTransport())
        gateway.start()
        self.assertIsNone(gateway.bound_port)

    def test_write_mode_requires_enabled_gateway(self):
        with self.assertRaises(GatewayConfigurationError):
            WebGatewayConfig(allow_writes=True).validate()

    def test_enabled_gateway_requires_credentials(self):
        with self.assertRaises(GatewayConfigurationError):
            WebGatewayConfig(enabled=True).validate()

    def test_password_file_takes_precedence_and_strips_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "password")
            path.write_text("from-file\n", encoding="utf-8")
            self.assertEqual(
                resolve_web_password(str(path), "from-environment"), "from-file"
            )


if __name__ == "__main__":
    unittest.main()
