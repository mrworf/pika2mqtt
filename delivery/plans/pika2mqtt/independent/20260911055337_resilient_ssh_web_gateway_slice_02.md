# Slice 02: Authenticated installer web gateway

## Goal and observable outcome

An operator can opt into an authenticated container endpoint that renders and
uses the complete installer site through the SSH tunnel. It is read-only by
default and forwards write methods only after a separate opt-in.

## Scope

- Add a concurrent reverse HTTP gateway in front of the loopback SSH tunnel.
- Add Basic Authentication, read-only enforcement, write opt-in, configuration,
  failure responses, logging, and automated tests.

## Non-scope

- TLS termination, Internet exposure, or user management.
- Changes to the inverter application or MQTT output.

## Dependencies and ordering

Requires the supervised tunnel and health interface delivered in slice 01.

## End-to-end behavior and state

- With web access disabled, create no listening socket.
- When enabled, require a username and a password from `WEB_PASSWORD_FILE` or
  the lower-precedence `WEB_PASSWORD` environment variable.
- Authenticate with constant-time comparison and never forward the gateway's
  Authorization header upstream.
- Preserve origin paths and queries, request bodies and non-hop-by-hop headers,
  response statuses and headers, MIME types, redirects, and streamed bodies.
- Permit GET, HEAD, and OPTIONS in read-only mode. Deny other methods with 405.
- When writes are enabled, forward all ordinary installer methods and bodies.
- Use a threaded server so a slow request does not block other browser assets.
- Return 503 while the tunnel is unavailable and 502 for an upstream transport
  failure; report such failures to the tunnel supervisor.

## Authorization and security

Authentication is mandatory whenever the gateway runs. Malformed credentials
receive 401 with a Basic challenge. Absolute-form or invalid upstream paths are
rejected so the gateway cannot become an open proxy.

## Validation and errors

Invalid option combinations and missing credentials fail startup. Request logs
are DEBUG-level and redact authorization values.

## Implementation surfaces

Expected changes include a gateway module, entry-point configuration/lifecycle,
and HTTP integration tests using a local fake installer server and fake tunnel
health object.

## Tests and commands

- Positive: authenticated GET/HEAD/OPTIONS, concurrent requests, POST and an
  arbitrary method in write mode, headers, query strings, forms, redirects,
  downloads, and password-file precedence.
- Negative: disabled listener, missing/invalid authentication, writes in
  read-only mode, invalid path, disconnected tunnel, and upstream failure.
- Run: `python3 -m unittest discover -s tests -v`
- Run: `python3 -m py_compile pika2mqtt.py pika_transport.py web_gateway.py`

## Acceptance criteria and commit boundary

The gateway can exercise the installer UI without blocking unrelated requests,
enforces authentication and method policy, passes its focused/full tests, and
is committed with only slice-owned code, tests, and its plan artifact.
