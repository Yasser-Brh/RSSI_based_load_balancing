# RSSI-Based Load Balancing Algorithm

A Python closed-loop control system that polls two Wi-Fi 7 Access Points (APs) running [Openwrt](https://openwrt.org/) through the [OpenWISP](https://openwisp.io) REST API, measures the RSSI of each connected station, and disassociates the weakest-signal clients from the more-loaded AP to encourage them to roam to the less-loaded one.

## What it does

- Fetches a token from OpenWISP or uses static token from .env
- Polls two APs through monitoring GET endpoints
- Stores AP, radio, station, and control-action data in SQLite database
- Runs a RSSI based load balancig algorithm
- Sends POST commands to APs through the OpenWISP controller
- Exports plotting-ready CSV

## Policy implemented

- Monitor all radio bands shared by AP1 and AP2
- Compare the number of **active** clients (those reporting a non-`null` RSSI) on each AP
- If the imbalance is at least `OPENWISP_CLIENT_DIFF_THRESHOLD`, select the more-loaded AP
- Sort its clients by ascending RSSI (weakest signal first — most likely to roam)
- Disassociate `⌊diff / 2⌋` clients via `hostapd_cli disassociate <mac>`
- Wait `OPENWISP_CONTROL_COOLDOWN` seconds for stations to reassociate before re-evaluating

## Project layout

- `src/config.py`: loads environment-based configuration
- `src/client.py`: token, monitoring GET, command POST, Wi-Fi sessions GET
- `src/controller.py`: snapshot normalization and balancing logic
- `src/storage.py`: SQLite schema and export helpers
- `src/cli.py`: command-line entry point

## Configuration

Copy `.env.example` to `.env` and fill in your own values.

Required variables:

- `OPENWISP_USERNAME` or `OPENWISP_TOKEN` (static Bearer token)
- `OPENWISP_PASSWORD` (if not using a static token)
- `AP1_DEVICE_ID`
- `AP1_DEVICE_KEY`
- `AP2_DEVICE_ID`
- `AP2_DEVICE_KEY`
- At least one radio band per AP, e.g.:
  - `AP1_RADIO_5G_IFACE` / `AP1_RADIO_5G_UCI`
  - `AP2_RADIO_5G_IFACE` / `AP2_RADIO_5G_UCI`

See `.env.example` for all available variables and their defaults.

## Commands

Initialize the database:

```bash
python3 -m src.cli init-db
```

Collect one monitoring snapshot from both APs:

```bash
python3 -m src.cli collect --once
```

Collect snapshots for 10 minutes (baseline):

```bash
python3 -m src.cli collect --duration 600 --interval 5
```

Run the balancing loop in dry-run mode:

```bash
python3 -m src.cli run --duration 600 --interval 5
```

Run the balancing loop and actually apply POST commands:

```bash
python3 -m src.cli run --duration 600 --interval 5 --apply
```

Sync Wi-Fi session history into SQLite:

```bash
python3 -m src.cli sync-sessions --start "2026-03-30T07:00:00Z" --stop "2026-03-30T09:00:00Z"
```

Export a plotting-ready CSV for one radio:

```bash
python3 -m src.cli export-csv --interface phy0.1-ap0 --output exports/phy0_1_ap0.csv
```

## Suggested experiment for the paper

- AP1 and AP2 use the same SSID
- Run a baseline collection without control for 10 to 15 minutes
- Run the control loop with `--apply` for 10 to 15 minutes
- Export CSV and plot:
  - Number of associated stations per AP over time
  - RSSI (dbm) per station over time

## SQLite content

The database stores:

- `poll_snapshots`: raw AP snapshots
- `radio_snapshots`: radio-level metrics per polling instant
- `station_snapshots`: client-level metrics per radio snapshot
- `control_actions`: POST actions and responses
- `wifi_sessions`: Wi-Fi session history fetched from OpenWISP

## Notes

- keep secrets only in environment variables or `.env` (never commit credentials)
- rotate any credentials or tokens that were previously exposed
- SSL verification is disabled by default (`OPENWISP_VERIFY_SSL=false`); set it to `true` if your server has a valid certificate chain
