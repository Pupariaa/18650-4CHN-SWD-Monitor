# WebSocket Protocol (schema version 1)

Date: 2026-11-07

`chn4-bridge serve` streams the four channels of the charger as JSON messages over a WebSocket. This document specifies version 1 of the message format.

## Current production rule

- **Read-only.** The server never acts on client messages. There is no message to start, stop or configure a charge, change a setting or write anything to the device. Any message received from a client is answered with an `error` of code `read_only` and otherwise ignored.
- **One acquisition, many clients.** A single SWD session feeds every client. Connecting a client never opens a new debug session.
- **Local by default.** The server binds to `127.0.0.1:8765`. A non-loopback address requires `server.allow_remote: true` or `--allow-remote`.

## Connection

| Item | Value |
|---|---|
| URL | `ws://127.0.0.1:8765` (no path required) |
| Subprotocol | none |
| Frames | text frames, one JSON object per frame, UTF-8 |
| Heartbeat | the server sends WebSocket pings every `heartbeat_interval_s` (20 s) and closes the connection if no pong arrives within `heartbeat_timeout_s` (20 s). Standard WebSocket clients answer pings automatically. |
| Client limit | `max_clients` (16). Extra connections are closed with code `1013` (try again later). |
| Inbound limit | client messages larger than 4096 bytes close the connection (code `1009`). |
| Origins | when `server.allowed_origins` is set, browser connections from other origins are rejected during the handshake. Non-browser clients (no `Origin` header) are always accepted. |

On connection the server sends, in order:

1. `hello`,
2. `probe_status` with the current state of the source,
3. `snapshot` with the latest snapshot, if one exists.

After that, a `snapshot` is sent every `broadcast_interval_s` (1 s by default) when a new capture is available, and `probe_status` and `error` messages are sent when they occur. A client never receives the same snapshot twice: if no new capture was produced (probe disconnected, replay finished), nothing is sent until there is one.

## Envelope

Every message has the same four keys:

```json
{
  "schema_version": 1,
  "type": "snapshot",
  "timestamp_utc": "2026-11-07T14:31:16.099Z",
  "data": {}
}
```

| Key | Type | Description |
|---|---|---|
| `schema_version` | integer | Version of this specification. Always `1` here. |
| `type` | string | `hello`, `snapshot`, `probe_status`, `error` or `shutdown` |
| `timestamp_utc` | string | When the server built the message, ISO 8601 UTC with milliseconds and a `Z` suffix |
| `data` | object | Type-specific payload, described below |

All timestamps in the protocol use the same format. Numbers are JSON numbers; `NaN` and infinity are never emitted. Missing or unavailable values are `null`.

## `hello`

Sent once, first, on every connection.

```json
{
  "schema_version": 1,
  "type": "hello",
  "timestamp_utc": "2026-11-07T14:31:15.487Z",
  "data": {
    "protocol": "chn4-bridge",
    "schema_version": 1,
    "server_version": "0.1.0",
    "read_only": true,
    "source": {"kind": "replay", "description": "replay of capture", "simulated": false},
    "channel_count": 4,
    "broadcast_interval_s": 1.0,
    "stale_after_s": 5.0,
    "include_experimental": false,
    "units": {"voltage_v": "V", "display_voltage_v": "V", "capacity_mah": "mAh", "energy_mwh": "mWh", "elapsed_s": "s", "resistance_raw_mohm": "mOhm", "resistance_compensated_mohm": "mOhm", "current_recent_a": "A", "current_average_a": "A", "stop_threshold_v": "V", "adc_raw": "counts"},
    "field_quality": {"present": "direct", "state_code": "direct", "state": "experimental", "activity": "estimated", "voltage_v": "experimental", "adc_raw": "direct", "display_voltage_v": "direct", "capacity_mah": "direct", "energy_mwh": "direct", "elapsed_s": "direct", "resistance_raw_mohm": "direct", "resistance_compensated_mohm": "experimental", "current_recent_a": "estimated", "current_average_a": "estimated", "stop_threshold_v": "experimental", "loop_count": "experimental"}
  }
}
```

| Key | Description |
|---|---|
| `protocol` | Always `chn4-bridge` |
| `schema_version` | Same as the envelope |
| `server_version` | Version of the `chn4-bridge` package |
| `read_only` | Always `true` in version 1 |
| `source.kind` | `hardware`, `replay` or `simulation` |
| `source.description` | Human-readable description (probe, file name, ...) |
| `source.simulated` | `true` when the data is synthetic. Simulated data must never be presented as measurements. |
| `channel_count` | Always `4` |
| `broadcast_interval_s` | Snapshot period configured on the server |
| `stale_after_s` | Age after which a snapshot is flagged `stale` |
| `include_experimental` | Whether snapshots contain the `experimental` block |
| `units` | Unit of each numeric channel field |
| `field_quality` | Quality of each channel field, see [Measurement quality](#measurement-quality) |

## `snapshot`

The four channels decoded from one capture.

```json
{
  "schema_version": 1,
  "type": "snapshot",
  "timestamp_utc": "2026-11-07T14:31:16.099Z",
  "data": {
    "sequence": 2,
    "captured_at": "2026-11-07T14:31:15.929Z",
    "recorded_at": "2026-11-07T02:30:09.000Z",
    "source": "replay",
    "simulated": false,
    "consistency": "unverified",
    "read_duration_ms": null,
    "age_s": 0.156,
    "stale": false,
    "channels": [
      {
        "channel": 1,
        "present": true,
        "state_code": 1,
        "state": "charge",
        "activity": "active",
        "voltage_v": 3.4641,
        "adc_raw": 3224,
        "display_voltage_v": 3.464127,
        "capacity_mah": 11.77676,
        "energy_mwh": 40.68318,
        "elapsed_s": 48,
        "resistance_raw_mohm": 48.0,
        "resistance_compensated_mohm": null,
        "current_recent_a": 0.8755,
        "current_average_a": 0.8833,
        "stop_threshold_v": 2.8,
        "loop_count": 1,
        "issues": []
      }
    ]
  }
}
```

The example is the second frame of the reference capture replayed by the server; `channels` is shortened to one entry here and always holds four entries, ordered by channel number.

### Snapshot fields

| Key | Type | Description |
|---|---|---|
| `sequence` | integer | Increases by one for every capture since the server started. Gaps mean the client missed snapshots. |
| `captured_at` | string | When the capture was taken (host clock). For a replay, when the frame was replayed. |
| `recorded_at` | string or null | For a replay, when the frame was originally recorded. `null` for live data. |
| `source` | string | `hardware`, `replay` or `simulation` |
| `simulated` | boolean | `true` for synthetic demo data |
| `consistency` | string | Torn-read check: `consistent`, `inconsistent` or `unverified` (check disabled or offline data) |
| `read_duration_ms` | number or null | Duration of the SWD reads for this capture |
| `age_s` | number | Age of the capture when the message was built |
| `stale` | boolean | `true` when `age_s` exceeds `stale_after_s` |
| `channels` | array | Four channel objects |

### Channel fields

| Key | Type | Unit | Quality | Description |
|---|---|---|---|---|
| `channel` | integer | | | 1 to 4 |
| `present` | boolean | | direct | Battery detected by the firmware |
| `state_code` | integer | | direct | Raw state byte |
| `state` | string | | experimental | `stop` (0), `charge` (1), `discharge` (2) or `unknown` |
| `activity` | string | | estimated | `active` (counters progressing), `idle` (counters stopped), `absent` or `unknown` (not enough history yet) |
| `voltage_v` | number or null | V | experimental | Battery voltage from the ADC buffer, updated for all four slots. Uncalibrated scale. |
| `adc_raw` | integer or null | counts | direct | Raw ADC sample. Reported even for an empty slot. |
| `display_voltage_v` | number or null | V | direct | Voltage cached for the display; only refreshed for the slot selected on the device |
| `capacity_mah` | number or null | mAh | direct | Capacity counter of the current step |
| `energy_mwh` | number or null | mWh | direct | Energy counter of the current step |
| `elapsed_s` | integer or null | s | direct | Elapsed time of the current step |
| `resistance_raw_mohm` | number or null | mOhm | direct | Resistance stored by the firmware |
| `resistance_compensated_mohm` | number or null | mOhm | experimental | Raw resistance minus the per-channel offset configured on the server; `null` when no offset is configured |
| `current_recent_a` | number or null | A | estimated | Estimated from the capacity counter over the last seconds; `0.0` when idle |
| `current_average_a` | number or null | A | estimated | Mean current since the start of the step, from capacity / elapsed |
| `stop_threshold_v` | number or null | V | experimental | Probable discharge stop voltage setting |
| `loop_count` | integer or null | | experimental | Probable cycle count setting |
| `issues` | array of strings | | | Reasons why values were discarded or flagged, see below |
| `experimental` | object | | unknown | Only when `include_experimental` is enabled, see below |

When `present` is `false`, every measurement is `null` and `activity` is `absent`.

### Issue codes

| Code | Meaning |
|---|---|
| `adc_unavailable` | The source provided no ADC data (for example a bare channel block dump) |
| `adc_sample_invalid` | The ADC sample was zero, saturated or outside the plausible range; `voltage_v` is `null` |
| `capacity_mah_not_finite`, `energy_mwh_not_finite`, `resistance_raw_mohm_not_finite`, `display_voltage_v_not_finite` | The float in memory was NaN or infinite; the value is `null` |
| `capacity_mah_negative`, `energy_mwh_negative`, `resistance_raw_mohm_negative` | The float in memory was negative; the value is `null` |
| `counter_reset` | Elapsed time or capacity went backwards (new step); the current history was cleared |
| `host_clock_discontinuity` | Capture timestamps went backwards; the current history was cleared |
| `resistance_compensation_negative` | The configured offset is larger than the raw resistance; the compensated value is `null` |
| `presence_word_unexpected` | The presence word was neither 0 nor 1 |
| `state_code_unknown` | The state byte is not 0, 1 or 2 |

New issue codes may be added within version 1. Clients should display unknown codes as-is rather than reject them.

### Experimental block

With `server.include_experimental: true` (or `serve --experimental`) each channel carries the fields whose meaning is not established:

```json
"experimental": {
  "voltage_candidate_a_v": 3.403016,
  "voltage_candidate_b_v": 3.361917,
  "byte_0x02": 0,
  "word_0x08": 1,
  "word_0x14": 1,
  "word_0x20": 314661,
  "word_0x24": 0,
  "word_0x2c": 65906,
  "presence_word": 1,
  "raw_hex": "011c000130000000010000009c6d3c4194bb22420100000003cb5940a529574025cd04000000000041b45d40720101000000404201000000"
}
```

(CH1 of the second frame of the reference capture.)

`raw_hex` is the 56-byte channel structure in hexadecimal. The content of this block is for research and **is not covered by the compatibility policy**: keys may be renamed, promoted to regular fields or removed in any release.

## `probe_status`

Sent on connection and whenever the state of the source changes.

```json
{
  "schema_version": 1,
  "type": "probe_status",
  "timestamp_utc": "2026-11-07T14:31:16.922Z",
  "data": {
    "state": "finished",
    "source": "replay",
    "description": "replay of capture",
    "message": "end of capture",
    "attempt": 0,
    "changed_at": "2026-11-07T14:31:16.922Z"
  }
}
```

| `state` | Meaning |
|---|---|
| `connecting` | Opening the source (attaching to the target) |
| `connected` | Captures are being produced |
| `disconnected` | The source failed; the server retries after `reconnect_delay_s`. `attempt` counts consecutive failures. |
| `failed` | The source failed permanently (retries exhausted or non-retryable error). The server sends `shutdown` and stops. |
| `finished` | The replay reached its end. The server keeps running and still serves the last snapshot. |
| `stopped` | The acquisition was stopped |

`message` holds a human-readable explanation or `null`.

## `error`

Non-fatal errors, and the fatal error that precedes a `failed` status.

```json
{
  "schema_version": 1,
  "type": "error",
  "timestamp_utc": "2026-11-07T14:31:15.488Z",
  "data": {"code": "read_only", "message": "this server is read-only and ignores client messages", "fatal": false}
}
```

| `code` | Meaning |
|---|---|
| `read_only` | The client sent a message; it was ignored |
| `probe_not_found` | No probe detected, or none matches the configured unique ID |
| `probe_selection` | Several probes match; one must be selected in the configuration |
| `probe_busy` | The probe is used by another program |
| `target_not_running` | The core is halted or locked up; the bridge never resumes it |
| `transport_error` | SWD communication failed (timeout, USB disconnection, fault) |
| `unsafe_environment` | A pyOCD user script is present in the working directory |
| `pyocd_unavailable` | pyOCD is not installed |
| `decode_error` | A capture could not be decoded |
| `internal_error` | Unexpected error |

`fatal: true` means the acquisition has stopped and a `shutdown` message follows.

## `shutdown`

Sent to every client just before the server closes the connections.

```json
{"schema_version": 1, "type": "shutdown", "timestamp_utc": "2026-11-07T14:31:18.936Z", "data": {"reason": "server_stopping"}}
```

| `reason` | Meaning |
|---|---|
| `server_stopping` | Normal stop (Ctrl+C, SIGTERM) |
| `acquisition_failed` | The source failed permanently |

The connection is then closed with code `1000` or `1001`.

## Backpressure and slow clients

Each client has its own outgoing queue of `client_queue_size` messages (8 by default). When the queue is full the **oldest** message is dropped, so a client that falls behind receives the most recent data and sees gaps in `sequence`. A single send that does not complete within `send_timeout_s` (5 s) closes the connection with code `1011`. Neither case affects the acquisition or the other clients.

## Measurement quality

| Quality | Meaning |
|---|---|
| `direct` | Read as-is from a firmware variable whose meaning was correlated with the device display. Not calibrated against an instrument. |
| `estimated` | Computed by the bridge from direct values (currents, activity). Can lag or differ from what the device shows. |
| `experimental` | Interpretation or scale not fully established (ADC voltage scale, state names, settings, compensated resistance) |
| `unknown` | Meaning not known (the `experimental` block) |

The current values are always estimates: no instantaneous current variable has been identified in the device memory. See [REVERSE_ENGINEERING.md](./REVERSE_ENGINEERING.md#7-current-estimation).

## Compatibility policy

Within `schema_version` 1:

- existing keys keep their name, type and meaning,
- new keys may be added to any object, new message types and new values for `state`, `issues`, `error.code` and `shutdown.reason` may be added,
- a field's quality may be upgraded (for example from `experimental` to `direct`) when new evidence supports it; `hello.field_quality` always tells the current value.

Clients must therefore ignore unknown keys and unknown message types, and should treat unknown enumeration values as opaque strings.

Any change that removes or renames a key, changes a type or changes a meaning increments `schema_version`. Clients should check `hello.schema_version` and refuse versions they do not know.

The `experimental` block is excluded from these guarantees.

## Security

The protocol has no authentication and no encryption. Keep the default loopback binding unless you need remote access. When exposing the server with `--allow-remote`, anyone who can reach the port can read the measurements (but cannot control anything). For browser clients, set `server.allowed_origins` to the origins of the pages allowed to connect, for example `["http://localhost:8000"]`.

## Clients

- [`examples/ws_client.py`](../examples/ws_client.py): Python, using `websockets`.
- [`examples/ws_client.mjs`](../examples/ws_client.mjs): Node.js 22 or newer, using the built-in `WebSocket`.
- [`examples/dashboard.html`](../examples/dashboard.html): a single HTML page for the browser.
