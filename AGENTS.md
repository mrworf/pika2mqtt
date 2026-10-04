# MQTT discovery validation

Home Assistant device-discovery removals must contain the component's original
`platform`: for example `{"platform": "binary_sensor"}`. An empty `{}` is invalid,
not a valid removal marker. Exclude platform-only removals from cached definitions.

For discovery changes, validate every published device message in the fake MQTT
client against the platform/unique-ID contract as well as testing state templates.
Cover intermediate and retained messages, HA offline/reconnect, cached manifests,
and firmware pruning. Mock publication success alone does not prove HA accepts a
payload. Full tests: `python -m unittest discover -s tests -v` (loopback required).
