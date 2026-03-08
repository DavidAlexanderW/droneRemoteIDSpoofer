# Scenario Config

This document describes the JSON scenario file used by `spoof_drones.py`.
CLI flags override config values.

## Top-level structure
```json
{
  "global": { ... },
  "drones": [ ... ]
}
```

## Global fields
- `interface` (string): network interface for injection. Default: `wlan1`.
- `interval` (number): seconds between transmissions. Default: `1.0`.
- `location` ([lat, lng]): base coordinates in decimal degrees. Default: Zurich.
- `random` (int): number of random drones if `drones` is empty. Default: `1`.

## Drone fields
Each entry in `drones` describes a single drone. Missing fields are generated
randomly (serial, MAC, and locations).

- `mode` (string): `random`, `static`, or `waypoints`. Default: `random`.
- `serial` (string): max 20 chars. Optional.
- `mac` (string): MAC address. Optional.
- `start_location` ([lat, lng]): initial drone location. Optional.
- `pilot_location` ([lat, lng]): pilot position. Optional.
- `lifespan_seconds` (int): stop transmitting after N seconds. Optional.
- `waypoints` (list): required when `mode` is `waypoints`.
  - Each waypoint is `[lat, lng, hold_seconds?]`.
  - `hold_seconds` defaults to `0` when omitted.

## Modes
### `random`
Drone performs a random walk around its current position each interval.

### `static`
Drone stays at its starting position.

### `waypoints`
Drone jumps to each waypoint in order and holds for `hold_seconds`. After the
last waypoint, it stays at the final location.

## Examples
Minimal:
```json
{
  "global": { "interface": "wlan1" },
  "drones": [ { "mode": "random" } ]
}
```

Waypoints:
```json
{
  "drones": [
    {
      "mode": "waypoints",
      "waypoints": [
        [47.3764, 8.5313, 2],
        [47.3766, 8.5316, 2],
        [47.3768, 8.5319, 2]
      ]
    }
  ]
}
```

Full template:
- See `scenario.template.json`.
