# govee_ble_lights

Home Assistant custom integration for Govee lights over Bluetooth LE.

Fork of [Beshelmek/govee_ble_lights](https://github.com/Beshelmek/govee_ble_lights)
with the BLE path rewritten around held connections. Upstream has been inactive
since mid-2025; this fork is maintained for the hardware listed below.

## What differs from upstream

**Held connections with keep-alive.** Govee firmware drops idle BLE clients
after ~10 s and a connect costs 0.6–4 s, so connect-per-command gives
multi-second latency. This fork holds the link for 600 s after the last
command, sends a keep-alive frame every 4 s, and reconnects once on send
failure. Measured command latency: ~0.6 s cold, **17–23 ms warm**
(H605C at −50 RSSI direct; H601C via an ESPHome-API Bluetooth proxy).

Other changes:

- Per-model brightness scaling: H601x takes 0–100 (clamped), others raw 0–255.
- H601C/H601D support: catalog fallback to H601B, two-level scene catalog
  flattening — H601D exposes its 61 named scenes (upstream showed one blank entry).
- Failed sends raise `HomeAssistantError` instead of silently returning `None`.
- Connect attempts capped at 1 — `bleak-retry-connector` already retries
  internally; the previous 3× wrapper hung for minutes on unreachable devices.
- Config flow pre-selects the model from the advertised name (`ihoment_H601C_xxxx`).
- Catalog load failure degrades the entity instead of killing it.
- 35 unit tests, runnable without a Home Assistant install (`pytest`).

## Verified hardware

| Model | Status |
|---|---|
| H601C / H601D (Glide downlights) | brightness 0–100, scenes, no status readback |
| H605C | brightness raw 0–255 |

Basic control (power/brightness/color) uses the generic `0x33` frame protocol
and should work on most BLE-capable Govee lights; scene support depends on the
model catalog. Other models are untested. Issues are welcome with the model
number and a debug log, but this repository is maintained as-is for the
hardware above.

## Requirements

- A working Home Assistant Bluetooth stack: local adapter or an ESPHome
  Bluetooth proxy in range of the fixtures.
- Addressing note: on `60:74:F4`-prefixed devices the BLE MAC is the
  WiFi MAC + 1. The device must be powered (wall switches off = BLE dead).

## Installation

### HACS (custom repository)

1. HACS → ⋮ → *Custom repositories*
2. Add `https://github.com/morgan-saga/govee_ble_lights`, category *Integration*
3. Install, restart Home Assistant
4. Settings → Devices & services → Add integration → Govee BLE lights

### Manual

Copy `custom_components/govee-ble-lights` into `/config/custom_components`
and restart Home Assistant.

## Behavior notes

- BLE entities are optimistic: H601x firmware has no known status readback,
  so entity state reflects the last command sent, not the device.
- A held connection occupies an adapter/proxy slot for up to 600 s after the
  last command. Size your proxy's max-connections to the number of fixtures
  you control concurrently.
- The Govee cloud/API path from upstream is unchanged.

## License

MIT, retained from upstream. Protocol groundwork by the original project and
community reverse engineering.
