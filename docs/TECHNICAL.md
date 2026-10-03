# Technical reference

[Home Assistant setup and capabilities](../README.md) · [Upgrade guide](MIGRATION.md)

This guide covers MQTT payloads, firmware definitions, fault policy and installer
API behavior. Most Home Assistant users can use discovered entities without editing
MQTT configuration.

## Firmware register diagnostics and reference

The container reads the installed SunSpec XML under
`/opt/pika/sunspec-models/smdx/` through fingerprint-verified SSH. Numeric enum values
and bit positions come from those files, including sparse bitfields. Definitions
refresh after SSH reconnects and detected firmware changes; loading failures retry
with backoff and jitter capped at 60 seconds without blocking telemetry polling.
Temporary SSH failures retain validated definitions. A detected firmware change
suspends interpretation until replacement definitions load. Conflicting definitions
and ambiguous symbol positions remain unavailable rather than being guessed.

Existing inverter, battery and PV Link devices gain these MQTT-discovered diagnostics:

- **Last event**: the most recently reported event, with its raw code, symbol,
  available description, supported states and definition checksum as attributes.
  It is historical and does not contribute to active faults. Repeated occurrences
  of the same event cannot be detected from this register alone.
- **Active error count**: count of classified active error indicators, with the
  names and coverage status as attributes. It becomes unavailable if required
  assessment registers are missing, stale, ambiguous or contain unknown values.
  Several indicators may describe the same underlying physical problem.
- **Individual flags**: disabled-by-default diagnostic binary sensors for every
  documented, non-reserved bit. Enable the desired entities on the device page to
  use them in dashboards and automations. Error/warning flags use the problem
  device class; ordinary flags retain their positive meaning, such as Heartbeat Good.
- **Register definitions available**: a service diagnostic with source, checksum,
  firmware versions and the latest load error as attributes.

Status and self-test sensors include supported states and current interpretation as
attributes. Extra enum sensors are disabled by default. Numeric raw-model diagnostics
remain available. The `decoded_registers` object in each device state topic contains
raw values, active symbols, descriptions, unknown-bit masks and interpretation
availability. Stable flag identifiers use serial/model/register/bit position so
firmware label changes do not replace entities. Temporary missing data does not
remove entities; firmware definition changes reconcile obsolete generated components.

PV Link Fault and Fault summary use corrected firmware error bits and PVRSS lockout
interpretation. Core fault monitoring still works from a valid controller directory
when detailed PV models fail. A normal core status does not prove that all detailed
error registers have been checked: Detailed fault data unavailable and Active error
count make that coverage distinction visible.

The complete reference lives at **`/data/register_reference.md`** inside Docker. With
`-v /your/path:/data`, read `/your/path/register_reference.md` on the Docker host.
When `PV_INVENTORY_FILE` is customized, reference and manifest files live beside it.
The file describes the last successfully loaded definitions; check its reported
firmware and checksum when investigating an upgrade.

With the optional web gateway enabled and its usual credentials configured, visit:

- `http://<docker-host>:8000/diagnostics/registers` for an HTML reference.
- `http://<docker-host>:8000/diagnostics/registers.md` to download the Markdown file.

These routes require the same authentication as the installer gateway and are always
read-only, even with `WEB_WRITE_ENABLED=true`. They use local reference data and work
through a temporary tunnel outage. Before definitions load, or during firmware
replacement, they return HTTP 503. Web access remains disabled unless `WEB_ENABLED=true`
and the gateway port is published.

The reference lists every supported enum state/flag, numeric value or bit position,
firmware description and application-policy classification. Many firmware entries
provide only a symbol name; those are explicitly marked **No description provided by
firmware**. Policy classification is separate from the firmware's documentation.
The [Generac PWRcell inverter installation and owner's manual](https://www.generac.com/globalassets/residential/dealers--installers/generac-installer-programs/solar--battery-installer-support/a0001424068-rev-j-1o-pwrcell-inverter-install-and-owners-manual.pdf)
provides operating and troubleshooting context but does not define every internal
register. Consult the reference from your own firmware for the available values.
The firmware bundle can include models for hardware you do not have. Home Assistant
entities are generated only for models and records collected for your devices.

For an automation, enable the relevant binary sensor, select it using Home Assistant's
entity picker, and trigger on `off` → `on` for an error (or `on` → `off` for a positive
health flag). Use `for:` to require a sustained condition, and handle `unavailable`
separately. An unavailable reading is not a cleared fault. Check supported states
before comparing an enum sensor's state in a template.

Severity comes from the bundled [register_policy.json](../register_policy.json), not
hard-coded firmware numbers. To customize it, copy the entire file, mount the copy
read-only, set `REGISTER_POLICY_FILE` to its container path, and restart the container.
The CLI equivalent is `--register-policy-file`. All six version-1 fields are required:
`version`, `error_registers`, `bitfields`, `assessment_registers`, `errors`, and `warnings`.
The policy uses model/register names and firmware symbols. `bitfields` resolves the
documented directory `Rb` field whose XML type is integer despite bit-position
symbols. `assessment_registers` defines the coverage needed for each device kind's
error count. Invalid policies fail startup with an explanatory log.

Container logs record definition loads/checksums and initial decoded observations.
Subsequent state/event/flag changes are INFO; classified errors, warnings and unknown
indicators are WARNING; error clearance is INFO. Unchanged observations are suppressed.
After stale data, a recovered observation is logged without claiming when a transition
happened. Event log timestamps are observation times, not inverter event timestamps.
Set `DEBUG=--debug` to include decoded register details and repeated loader diagnostics.

## MQTT and Home Assistant

All state, availability, and discovery messages use QoS 1 and retained
payloads. The broker last will and graceful shutdown payload are both
`disconnected`; a successful MQTT session publishes `connected`. The publisher
automatically reconnects with bounded backoff and republishes state and
discovery after reconnect or a `homeassistant/status` birth message.

State is normalized JSON under:

```text
house/energy/state/system
house/energy/state/inverter
house/energy/state/grid
house/energy/state/battery/000100080701
house/energy/state/battery/000100080701/module/1
house/energy/state/pv/00010003119C
```

When control is explicitly enabled, commands use:

```text
house/energy/command/system_operating_mode
house/energy/command/pv/00010003119C/enabled
```

Availability uses:

```text
house/energy/availability/service
house/energy/availability/inverter
house/energy/availability/pv/00010003119C
house/energy/availability/pv/00010003119C/enabled
house/energy/availability/control/pv/00010003119C
house/energy/availability/battery/000100080701/modules
house/energy/availability/battery/000100080701/module/1
house/energy/availability/power/solar
house/energy/availability/power/pv/00010003119C
```

Power-specific availability uses `available` and `unavailable`. It lets Home
Assistant suppress only a bad power measurement without treating the inverter
or PV Link as disconnected.

Home Assistant discovery creates one PWRcell inverter/system device, a battery
child, a child for each battery module, and one child per learned PV Link.
Principal power, battery, energy, status, connectivity, SnapRS, and PVRSS
measurements are exposed directly. The
inverter includes an enabled `System Operating Mode` sensor decoded from
`SysMd` (for example, `Clean Backup`), while its stable enum key, numeric code,
and model description remain available in the inverter JSON.
Every scalar supplied by the detailed installer models is also available as a
disabled-by-default diagnostic entity.

Battery module data comes from the inverter's `lithium_ion_string_module`
model. Each module child exposes state of charge and state of health by
default. Its physical cell count and minimum, maximum, and average cell voltage
and temperature are available as disabled-by-default diagnostics. A module is
a replaceable PWRcell battery module containing multiple physical cells; the
installer API does not expose SoC or SoH for each physical cell.

The existing aggregate PWRcell battery device and state topic are unchanged.
Module measurements use separate state and availability topics. If the model
is stale, all known module children become unavailable and stale measurements
are not republished. If the model reports an expected module number without a
corresponding record, that child remains visible but unavailable, making a
missing module distinguishable from a module that was never discovered.

## Optional operating mode and PV Link control

All write controls are disabled by default. To add a separate Home Assistant
`System Operating Mode Control` selector and an `Enabled Control` switch to
each PV Link, set:

```yaml
environment:
  OPERATING_MODE_CONTROL_ENABLED: "true"
```

The selector permits only `Grid Tie`, `Self Supply`, `Clean Backup`, and
`Priority Backup`. Safety Shutdown, Remote Arbitrage, and Sell remain readable
but cannot be commanded through pika2mqtt. The existing read-only System
Operating Mode sensor remains available for dashboards and automations.

Each PV Link switch sends an exact, non-retained `ON` or `OFF` command. The
existing read-only `Enabled` binary sensor is unchanged. It prefers the
controller's fresh device-directory state and falls back to `pvlink_status`
when the directory is unavailable; the control switch itself becomes
unavailable unless the service, inverter, PV Link, and authoritative directory
mapping are all available.

pika2mqtt does not assume that a PV Link's Modbus ID is its writable directory
block. Before every command it reads `/device/1/model/REbus_dir/devices` and
requires one exact match using the current Modbus unit ID plus the manufacturer,
device type, and device ID encoded in the 12-hex RCP serial. An already-matching
state is a write-free no-op. Otherwise pika2mqtt sends one form POST to that
same repeating-block route using the verified one-based `<block>_Ena` field
with value `1` or `0`. It does not retry the write.

After a write, the directory is read immediately and every five seconds for up
to 30 seconds. The identity is re-resolved each time, and the new state is
published only after the same mapping reports the requested value. Missing,
stale, malformed, changed, or ambiguous mappings are rejected and logged;
failure preserves the last confirmed state.

Home Assistant sends a non-retained command and pika2mqtt rejects any retained
command received on the topic. The displayed selection is not changed
optimistically. pika2mqtt sends one `0_SysMd` fixed-block form POST to the
writable system controller model at `/device/1/model/REbus_dir`; it never
writes the read-only `inverter_status` model. It reads the controller and
inverter status immediately, then every five seconds for up to 30 seconds, and
publishes the new state only after both independently report the requested
mode. Failed or unconfirmed commands are logged and the write is not retried.

For historical control bugs affecting older images, see the [upgrade guide](MIGRATION.md#historical-operating-mode-control-fixes).

Anyone who can publish to a command topic can change system behavior. Use MQTT
broker ACLs so only the intended Home Assistant account can publish to
`house/energy/command/system_operating_mode` and
`house/energy/command/pv/+/enabled`; other consumers should receive read-only
access.

Actual mode or PV Link changes are intentionally not exercised against a live
inverter by the automated tests. After enabling the feature, the system owner
should manually verify operating-mode control as follows:

1. Record the current mode and watch `docker logs -f pika2mqtt`.
2. Select one of the four approved modes in Home Assistant.
3. Confirm the log reports the command as confirmed and both the selector and
   read-only sensor show the chosen mode.
4. Confirm the installer interface and inverter display agree, then observe the
   selection for at least ten minutes and several telemetry refreshes.
5. Restore the preferred operating mode if the test used a temporary setting.

Do this only when changing the inverter mode is operationally safe. The MQTT
control flag is independent of `WEB_WRITE_ENABLED`; the installer web gateway
does not need to be exposed or write-enabled.

Verify each PV Link switch separately during safe daylight and load conditions:
watch the container log, toggle only the intended string, confirm both the
installer interface and the read-only Enabled entity agree, then restore the
desired state before proceeding to another string. Do not send commands to all
strings at once during initial validation.

## Measurements and availability

Power uses watts, battery charge uses percent, and energy uses kWh. Positive
grid power means export and positive battery power means discharge; separate
nonnegative import/export and charge/discharge sensors are also provided. Grid
import/export energy comes from the inverter's native `REbus_exp` `Whin` and
`Whx` counters. Other device energy counters are labeled accumulated energy
rather than being misrepresented as solar production.

PV Link production is accepted only when it is a finite value from 0 W through
5000 W inclusive. Negative values, higher spikes, non-finite values, and missing
samples are omitted rather than clamped or cached. The affected string power
sensor becomes unavailable while its other telemetry remains usable. If any
visible string has an invalid primary sample, aggregate solar power is also
omitted and unavailable instead of reporting a partial total. The container
logs one warning when a string enters an invalid-data episode and an
informational message when valid power resumes.

Each PV Link has independent communication, enabled, and fault signals.
Communication is based on `/devices` presence and `lastheard`; the
`Communication lost` entity changes only after 120 seconds by default. A PV
Link that is disabled but still responding therefore shows Enabled Off and
Communication lost OK. A detailed model returning HTTP 400/500 does not by
itself disconnect a string.

Detailed model values have a two-minute freshness window. A brief endpoint
failure continues to use the last confirmed value, but after that window only
the affected model-backed entities become unavailable. `/devices` power,
last-heard age, and communication monitoring remain usable. Cached raw model
payloads remain in MQTT JSON for diagnosis with `endpoint_health` freshness and
age, request duration, failure classification, consecutive-failure, and retry
metadata, but stale data is not used for normalized values or fault decisions.
The controller directory supplies each PV Link's core REbus status, voltage,
current, temperature, enabled state, and safe power fields. Core Fault remains
available from that source when optional PV models fail. Fresh optional models
add PV Link error bits, PVRSS lockout, and failed PVRSS self-tests; the
disabled-by-default `Detailed fault data unavailable` diagnostic identifies
reduced coverage. The system device provides separate aggregate “any string
disconnected” and “any string faulted” binary sensors for alerting.

## Polling and recovery

The collector follows the endpoint contract used by the installer UI:
`/devices`; controller `REbus_dir/devices`; inverter `common`, `REbus_status`,
`inverter_status`, `REbus_exp`, and `inverter`; battery `common`, `REbus_status`,
`battery` and `lithium_ion_string/lithium_ion_string_module`; and PV Link
`common`, `REbus_status`, `pvlink_status`, and
`pvrss_telemetry`. Detailed model
requests run serially in a separate worker with a persistent HTTP session and
a configurable 20-second timeout, so a slow or hung model cannot delay
`/devices` polling. The fast controller directory is prioritized when several
routes are due. Requests are spaced rather than sent as one synchronized
burst. Detailed model errors are classified and retried with staggered
per-route exponential backoff using a 15-minute base at the maximum cadence.
The installer does not provide `Retry-After`; directory `UpdtTm` values are
sample timestamps, not requested polling delays. Identical persistent failures
warn once until the error changes or the endpoint recovers.
HTTP 500 responses and read timeouts from an individual model affect only that
endpoint; they do not recycle a healthy SSH tunnel. Primary `/devices`
connection failures remain authoritative for tunnel-health recovery.

Set `HA_DISCOVERY_ENABLED=false` to consume the JSON topics without Home
Assistant discovery, for example through Telegraf or InfluxDB.
