"""Command-line entry point for the OpenWISP Wi-Fi control-loop demo.

Subcommands:
  init-db        Create the SQLite schema.
  collect        Poll monitoring snapshots and store them in SQLite.
  run            Run the RSSI-based balancing loop.
  sync-sessions  Pull Wi-Fi session history from OpenWISP into SQLite.
  export-csv     Export a radio time-series as a plotting-ready CSV.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .client import OpenWispClient
from .config import AppConfig, load_config
from .controller import RSSIBalancingController, get_radio_map, normalize_snapshot
from .storage import Storage


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def collect_iteration(
    client: OpenWispClient,
    storage: Storage,
    config: AppConfig,
) -> dict[str, object]:
    """Fetch one monitoring snapshot per AP, persist it, and return the normalised dict."""
    normalized_by_ap = {}
    for ap in config.aps:
        raw_snapshot = client.get_monitoring_snapshot(ap)
        normalized = normalize_snapshot(raw_snapshot)
        storage.insert_snapshot(
            timestamp=utc_now(),
            ap_id=normalized["ap_id"],
            ap_name=normalized["ap_name"],
            radios=normalized["radios"],
            raw_json=normalized["raw"],
        )
        normalized_by_ap[ap.name] = normalized
    return normalized_by_ap


def _print_snapshot(normalized_by_ap: dict, end: str = "\n") -> None:
    """Print a one-line human-readable summary of the current radio state."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    parts = []
    for ap_name, snap in normalized_by_ap.items():
        for r in snap["radios"]:
            if r.up:
                # Count only active clients (signal != None) — same logic as the controller.
                active = [c for c in r.clients if c.get("signal") is not None]
                n_active = len(active)
                n_total  = r.client_count
                ghost_str = f"+{n_total - n_active}ghost" if n_total > n_active else ""
                rssi_str = f"{r.avg_signal:.0f}dBm" if r.avg_signal is not None else "no-sta"
                parts.append(
                    f"{ap_name}/{r.interface_name}: "
                    f"{n_active}sta{ghost_str} rssi={rssi_str}"
                )
    line = f"[{ts}] " + (" | ".join(parts) if parts else "no radios up")
    print(line, end=end, flush=True)


def run_collect(args: argparse.Namespace, config: AppConfig) -> int:
    """Polling loop: collect monitoring snapshots and store them in SQLite."""
    client = OpenWispClient(config)
    storage = Storage(config.db_path)
    storage.init()
    tag = getattr(args, "tag", None)
    session_id = None
    if tag:
        session_id = storage.start_session(tag, "baseline", utc_now())
        print(f"[session] baseline '{tag}' started (id={session_id})")
    deadline = None if args.duration is None else time.time() + args.duration
    try:
        while True:
            normalized = collect_iteration(client, storage, config)
            _print_snapshot(normalized)
            if args.once:
                return 0
            if deadline is not None and time.time() >= deadline:
                return 0
            time.sleep(args.interval or config.poll_interval)
    finally:
        if session_id is not None:
            storage.end_session(session_id, utc_now())
            print(f"[session] baseline '{tag}' ended (id={session_id})")
        storage.close()


def run_control(args: argparse.Namespace, config: AppConfig) -> int:
    """Balancing loop: collect snapshots, decide, and optionally apply disassociate commands."""
    client = OpenWispClient(config)
    storage = Storage(config.db_path)
    storage.init()
    controller = RSSIBalancingController(config)
    tag = getattr(args, "tag", None)
    session_id = None
    if tag:
        session_id = storage.start_session(tag, "control", utc_now())
        print(f"[session] control '{tag}' started (id={session_id})")
    deadline = None if args.duration is None else time.time() + args.duration

    # Wait after each kick to let stations roam before re-reading the state.
    WAIT_AFTER_KICK = config.control_cooldown

    try:
        while True:
            snapshots = collect_iteration(client, storage, config)
            _print_snapshot(snapshots)
            action = controller.decide(snapshots)

            if action is not None:
                timestamp = utc_now()
                if args.apply:
                    response = client.execute_custom_command(action.ap.control_device_id, action.command)
                    status = "applied"
                else:
                    response = {"dry_run": True}
                    status = "dry_run"
                storage.insert_action(timestamp, action, status, response)
                macs_str = ", ".join(action.macs_to_disconnect)
                print(
                    f"\n  *** ACTION [{status.upper()}] {action.ap.name}/{action.band}: "
                    f"{action.action_type} {action.old_value}->{action.new_value} clients "
                    f"| MACs: {macs_str}"
                    f"\n      {action.reason}"
                    f"\n  ... waiting {WAIT_AFTER_KICK}s for stations to roam"
                )
                # Wait for stations to roam before re-reading the state.
                wait_end = time.time() + WAIT_AFTER_KICK
                while time.time() < wait_end:
                    if deadline is not None and time.time() >= deadline:
                        return 0
                    time.sleep(1)
            else:
                print(f"  [idle] {controller.last_skip_reason}")
                time.sleep(5)  # short sleep: re-poll every 5 s when idle

            if deadline is not None and time.time() >= deadline:
                return 0
    finally:
        if session_id is not None:
            storage.end_session(session_id, utc_now())
            print(f"[session] control '{tag}' ended (id={session_id})")
        storage.close()


def run_sync_sessions(args: argparse.Namespace, config: AppConfig) -> int:
    """Fetch Wi-Fi session history from OpenWISP and upsert it into SQLite."""
    client = OpenWispClient(config)
    storage = Storage(config.db_path)
    storage.init()
    filters = {}
    if args.device_id:
        filters["device"] = args.device_id
    if args.start:
        filters["start_time"] = args.start
    if args.stop:
        filters["stop_time"] = args.stop
    sessions = client.list_wifi_sessions(**filters)
    storage.upsert_wifi_sessions(sessions)
    print(json.dumps({"stored_sessions": len(sessions), "filters": filters}))
    storage.close()
    return 0


def run_export(args: argparse.Namespace, config: AppConfig) -> int:
    """Export the radio time-series to a CSV file suitable for plotting."""
    storage = Storage(config.db_path)
    storage.init()
    storage.export_radio_series_csv(args.output, interface_name=args.interface)
    storage.close()
    print(json.dumps({"output": str(args.output), "interface": args.interface}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenWISP Wi-Fi control-loop demo")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="Create SQLite schema")
    init_db.set_defaults(func=run_init_db)

    collect = subparsers.add_parser("collect", help="Collect monitoring snapshots")
    collect.add_argument("--once", action="store_true", help="Collect a single snapshot")
    collect.add_argument("--duration", type=int, help="Collection duration in seconds")
    collect.add_argument("--interval", type=float, help="Polling interval in seconds (float)")
    collect.add_argument("--tag", help="Experiment tag (e.g. 'exp1') — marks this as a named baseline session")
    collect.set_defaults(func=run_collect)

    control = subparsers.add_parser("run", help="Run the balancing loop")
    control.add_argument("--duration", type=int, help="Run duration in seconds")
    control.add_argument("--interval", type=float, help="Polling interval in seconds (float)")
    control.add_argument("--apply", action="store_true", help="Apply POST commands instead of dry-run mode")
    control.add_argument("--tag", help="Experiment tag (e.g. 'exp1') — must match the baseline tag")
    control.set_defaults(func=run_control)

    sync_sessions = subparsers.add_parser("sync-sessions", help="Sync Wi-Fi sessions into SQLite")
    sync_sessions.add_argument("--device-id", help="Optional device UUID filter")
    sync_sessions.add_argument("--start", help="Optional start_time filter")
    sync_sessions.add_argument("--stop", help="Optional stop_time filter")
    sync_sessions.set_defaults(func=run_sync_sessions)

    export = subparsers.add_parser("export-csv", help="Export radio time series to CSV")
    export.add_argument("--interface", help="Optional interface filter, e.g. phy0.1-ap0")
    export.add_argument("--output", required=True, type=str_to_path, help="CSV output path")
    export.set_defaults(func=run_export)

    return parser


def str_to_path(value: str):
    from pathlib import Path

    return Path(value)


def run_init_db(args: argparse.Namespace, config: AppConfig) -> int:
    """Initialise (or migrate) the SQLite database schema."""
    storage = Storage(config.db_path)
    storage.init()
    storage.close()
    print(json.dumps({"db_path": str(config.db_path), "status": "initialized"}))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments, load config, and dispatch to the appropriate subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config()
    return args.func(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
