# rtlamr-mqtt-bridge

[rtlamr](https://github.com/bemasher/rtlamr) → MQTT, **drop-in compatible with
[rtl_433](https://github.com/merbanan/rtl_433)'s MQTT output**.

If you read utility meters with an RTL-SDR and publish them to MQTT (typically
for [Home Assistant](https://www.home-assistant.io/)), you've probably used
rtl_433. This bridge publishes to the **same topics in the same format**, but
decodes with rtlamr — which, for Itron ERT and especially **Neptune R900 water
meters**, hears meters rtl_433 cannot. Swap the container; keep every sensor,
dashboard, and automation you already built.

## Why this exists

I spent days concluding my water meter's radio was dead. It wasn't — rtl_433
just couldn't decode it. Two distinct problems, observed mid-2026 (rtl_433
nightly) on a street full of Neptune R900s:

1. **Some R900 variants don't decode at all.** rtlamr heard my meter 15 times
   in 9 minutes; rtl_433 never decoded a single packet from it across hours of
   capture on the same antenna, while happily decoding other R900s nearby. If
   you've run water and watched zero meters move, this may be you.

2. **Older R900s encode consumption as BCD, and rtl_433 misparses them** —
   it renders the BCD digit-nibbles as a binary integer, i.e. it reads the
   decimal odometer *as hexadecimal*. The bug has a perfect signature:

   | true reading (BCD) | rtl_433 reports | because |
   |---:|---:|---|
   | 16,000 | 90,112 | `0x16000` = 90112 |
   | 104,100 | 1,065,216 | `0x104100` = 1065216 |
   | 189,395 | 1,610,645 | `0x189395` = 1610645 |

   These wrong readings look plausible and even move when the meter does, so
   you can chase them for a long time before noticing. (The same meters also
   report nonsense flag fields, e.g. `leak: 7`/`leak: 15` on every packet.)

rtlamr decodes both variants correctly (`r900` and `r900bcd` message types),
but speaks a different JSON dialect on stdout and has no MQTT support — so
everything downstream breaks if you switch. This bridge is the missing
adapter, plus one genuinely new trick:

**Automatic BCD detection.** When both `r900` and `r900bcd` are enabled, every
R900 transmission decodes under both parsers. The bridge pairs the two decodes
and decides — once per meter ID — which interpretation is real, using the
misparse identity above (and the fact that the BCD parser yields 0 when any
nibble exceeds 9). You get one event per transmission with the *correct*
consumption value, tagged `r900_encoding: bcd|binary`. Ambiguous meters can be
pinned with `R900_BCD_IDS` / `R900_BINARY_IDS`.

## Quick start

```yaml
# docker-compose.yml
services:
  rtlamr-bridge:
    image: ghcr.io/brad-richardson/rtlamr-mqtt-bridge:latest
    container_name: rtlamr-bridge
    restart: unless-stopped
    devices:
      - /dev/bus/usb          # your RTL-SDR
    network_mode: host
    environment:
      TZ: America/Vancouver   # timestamps are local time, like rtl_433
      MQTT_HOST: localhost
      MQTT_USER: rtl433
      MQTT_PASSWORD: "..."
      MQTT_RETAIN: "true"
      RTLAMR_MSGTYPE: r900,r900bcd
```

That's it — the container runs `rtl_tcp` and `rtlamr` internally and
supervises both. If you're replacing a `hertzg/rtl_433` service, keep your
broker settings and delete the rtl_433 `command:` line; the default topics
match rtl_433's common `-F mqtt://...,devices=rtl_433/devices[/model][/id],events=rtl_433/events`
layout.

## MQTT output

Identical layout to rtl_433:

```
rtl_433/events                                    full JSON, one message per packet
rtl_433/devices/<model>/<id>/<field>              one topic per field
rtl_433/bridge/status                             online/offline (LWT; bridge extra)
```

Example event for a Neptune R900 (fields named exactly as rtl_433 names them):

```json
{"time":"2026-06-12 19:46:33","model":"Neptune-R900","id":1582600001,
 "unkn1":163,"nouse":3,"backflow":0,"consumption":228633,"unkn3":0,
 "leak":0,"leaknow":0,"r900_encoding":"binary"}
```

Supported message types and the rtl_433 model names they map to:

| rtlamr `-msgtype` | rtl_433 model | notes |
|---|---|---|
| `r900`, `r900bcd` | `Neptune-R900` | paired & auto-resolved (see above) |
| `scm` | `ERT-SCM` | field-for-field match |
| `scm+` | `SCMplus` | field-for-field match incl. hex strings & `MeterType` |
| `idm`, `netidm` | `ERT-IDM` / `ERT-NetIDM` | best-effort (snake_cased), unverified against rtl_433 |

Honest differences from rtl_433: receiver-side fields (`rssi`, `snr`, `noise`,
`freq`, `mod`, `protocol`, `mic`) are not available from rtlamr and are
omitted; R900 `unkn2`/`extra` likewise. `r900_encoding` is added. If your
automations key on any of those, adjust accordingly.

Note rtlamr requires all enabled message types to share a symbol rate —
`scm`/`scm+`/`idm`/`r900` are compatible in one process on the same dongle.

## Configuration

| variable | default | purpose |
|---|---|---|
| `MQTT_HOST` / `MQTT_PORT` | `localhost` / `1883` | broker |
| `MQTT_USER` / `MQTT_PASSWORD` | — | broker auth (optional) |
| `MQTT_RETAIN` | `false` | retain all published messages |
| `MQTT_BASE_TOPIC` | `rtl_433` | base for the default topics below |
| `MQTT_EVENTS_TOPIC` | `<base>/events` | full-JSON event stream |
| `MQTT_DEVICES_TOPIC` | `<base>/devices/{model}/{id}` | per-field topic prefix |
| `MQTT_STATUS_TOPIC` | `<base>/bridge/status` | online/offline LWT |
| `RTLAMR_MSGTYPE` | `r900,r900bcd` | comma-separated rtlamr message types |
| `RTLAMR_FILTER_ID` | — | comma-separated meter IDs to pass through |
| `RTLAMR_CENTERFREQ` | rtlamr default (912.6 MHz) | tune the 2.4 MHz window |
| `RTLAMR_SERVER` | `127.0.0.1:1234` | rtl_tcp address |
| `RTLAMR_ARGS` | — | extra raw rtlamr flags |
| `RTL_TCP_SPAWN` | `true` | run rtl_tcp inside the container |
| `RTL_TCP_ARGS` | — | extra rtl_tcp flags (e.g. `-d 1 -g 40`) |
| `R900_MODE` | `auto` | `auto` \| `binary` \| `bcd` consumption decoding |
| `R900_BCD_IDS` / `R900_BINARY_IDS` | — | per-meter overrides for `auto` |
| `R900_PAIR_WINDOW` | `2.0` | seconds to wait for the paired decode |
| `RESTART_DELAY` | `5.0` | backoff when rtlamr/rtl_tcp exit |
| `WATCHDOG_TIMEOUT` | `300` | recycle rtl_tcp if no meter data for this many seconds (`0` disables) |
| `WATCHDOG_INTERVAL` | `30` | how often the watchdog checks data age |
| `RECYCLE_COOLDOWN` | `max(2×RESTART_DELAY, 10)` | min seconds between rtl_tcp recycles |
| `HEARTBEAT_FILE` | `/tmp/rtlamr-bridge.heartbeat` | last-message timestamp file backing the Docker healthcheck |
| `HEALTHCHECK_MAX_AGE` | `600` | healthcheck reports unhealthy when the heartbeat is older than this |

### Surviving USB re-enumeration

A USB SDR that re-enumerates (unplug/replug, power blip, hub reset) doesn't make
`rtl_tcp` exit — it keeps serving an empty sample stream, so `rtlamr` connects but
reads nothing and loops forever with no data. The bridge recovers automatically:
when `rtlamr` exits, or when no meter message arrives for `WATCHDOG_TIMEOUT`, it
recycles `rtl_tcp` so it re-opens the device. The image also ships a `HEALTHCHECK`
(backed by `HEARTBEAT_FILE`) so Docker / autoheal can restart the container if the
in-process recovery ever fails. Map the whole bus (`/dev/bus/usb:/dev/bus/usb`),
not a fixed `/dev/bus/usb/00X/0YY` node, so the new device node is visible after
re-enumeration.

## Home Assistant example

Neptune R900 `consumption` is the meter odometer; on many residential meters
one tick = 0.01 ft³ (verify against your dial!), so gallons = ticks × 0.000748 × 100:

```yaml
mqtt:
  sensor:
    - name: "Water Main Total"
      state_topic: "rtl_433/devices/Neptune-R900/1485200001/consumption"
      value_template: "{{ (value | float(0) * 0.0748) | round(1) }}"
      unit_of_measurement: "gal"
      device_class: water
      state_class: total_increasing
      force_update: true     # lets a derivative 'flow' sensor decay to zero
```

Add a [`derivative` sensor](https://www.home-assistant.io/integrations/derivative/)
on top for live flow rate, which is also a great leak detector.

## Identifying *your* meter (hard-won advice)

900 MHz carries: expect to hear 10–20 neighbors. Don't trust signal strength.
Confirm by exact dial↔radio match, or by running a known volume of water
(a bathtub is ~5.3 ft³) and watching which ID moves by that amount. And if no
meter moves — try this bridge before concluding your meter's radio is dead;
that's the exact wrong turn that led here.

## Development

```bash
pip install paho-mqtt pytest
pytest
```

The test suite pins the output shapes against real rtl_433 captures
(anonymized). PRs welcome — especially verified field mappings for IDM/NetIDM
and other message types, and HA MQTT discovery.

## Credits

- [bemasher/rtlamr](https://github.com/bemasher/rtlamr) does all the actual
  signal processing — this is just plumbing around it.
- [rtl_433](https://github.com/merbanan/rtl_433) defined the de-facto MQTT
  format this bridge targets, and decodes hundreds of devices rtlamr doesn't.
  Use both! (Two dongles, or pick per band.)

MIT licensed.
