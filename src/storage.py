"""SQLite persistence layer.

Schema overview:
  poll_snapshots      Raw per-AP snapshots (one row per polling instant).
  radio_snapshots     Per-radio metrics linked to a poll_snapshot.
  station_snapshots   Per-client metrics linked to a radio_snapshot.
  control_actions     Balancing actions taken (or simulated in dry-run mode).
  wifi_sessions       Wi-Fi session history fetched from the OpenWISP API.
  experiment_sessions Named experiment sessions (baseline / control phases).
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .controller import ControlAction, RadioState


SCHEMA = """
CREATE TABLE IF NOT EXISTS poll_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    ap_id TEXT NOT NULL,
    ap_name TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS radio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    interface_name TEXT NOT NULL,
    ssid TEXT,
    channel INTEGER,
    frequency INTEGER,
    tx_power INTEGER,
    noise INTEGER,
    htmode TEXT,
    up INTEGER NOT NULL,
    client_count INTEGER NOT NULL,
    avg_signal REAL,
    total_airtime INTEGER NOT NULL,
    rx_bytes INTEGER NOT NULL,
    tx_bytes INTEGER NOT NULL,
    rx_packets INTEGER NOT NULL,
    tx_packets INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES poll_snapshots(id)
);

CREATE TABLE IF NOT EXISTS station_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    radio_snapshot_id INTEGER NOT NULL,
    station_mac TEXT NOT NULL,
    vendor TEXT,
    signal INTEGER,
    rx_rate INTEGER,
    tx_rate INTEGER,
    rx_bytes INTEGER,
    tx_bytes INTEGER,
    rx_packets INTEGER,
    tx_packets INTEGER,
    airtime_rx INTEGER,
    airtime_tx INTEGER,
    raw_json TEXT NOT NULL,
    FOREIGN KEY(radio_snapshot_id) REFERENCES radio_snapshots(id)
);

CREATE TABLE IF NOT EXISTS control_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    ap_id TEXT NOT NULL,
    ap_name TEXT NOT NULL,
    interface_name TEXT NOT NULL,
    band TEXT,
    action_type TEXT NOT NULL,
    old_value INTEGER,
    new_value INTEGER,
    command_text TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT
);

CREATE TABLE IF NOT EXISTS wifi_sessions (
    id TEXT PRIMARY KEY,
    device_name TEXT NOT NULL,
    interface_name TEXT NOT NULL,
    ssid TEXT,
    station_mac TEXT NOT NULL,
    vendor TEXT,
    start_time TEXT,
    stop_time TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('baseline','control')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    note TEXT
);
"""


class Storage:
    """Thin wrapper around a SQLite connection providing typed insert/query helpers."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.connection = sqlite3.connect(str(db_path))
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        """Close the database connection."""
        self.connection.close()

    def start_session(self, tag: str, phase: str, timestamp: str, note: str = "") -> int:
        """Insert a new experiment session and return its id."""
        cur = self.connection.execute(
            "INSERT INTO experiment_sessions (tag, phase, started_at, note) VALUES (?, ?, ?, ?)",
            (tag, phase, timestamp, note),
        )
        self.connection.commit()
        return cur.lastrowid

    def end_session(self, session_id: int, timestamp: str) -> None:
        """Mark an experiment session as ended."""
        self.connection.execute(
            "UPDATE experiment_sessions SET ended_at = ? WHERE id = ?",
            (timestamp, session_id),
        )
        self.connection.commit()

    def list_sessions(self) -> list:
        """Return all experiment sessions ordered by start time desc."""
        return self.connection.execute(
            "SELECT * FROM experiment_sessions ORDER BY started_at DESC"
        ).fetchall()

    def init(self) -> None:
        """Create all tables if they do not already exist (idempotent)."""
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def insert_snapshot(self, timestamp: str, ap_id: str, ap_name: str, radios: list[RadioState], raw_json: dict) -> None:
        """Persist one polling snapshot with all its radio and station records."""
        cursor = self.connection.execute(
            "INSERT INTO poll_snapshots(timestamp, ap_id, ap_name, raw_json) VALUES (?, ?, ?, ?)",
            (timestamp, ap_id, ap_name, json.dumps(raw_json)),
        )
        snapshot_id = cursor.lastrowid
        for radio in radios:
            radio_cursor = self.connection.execute(
                """
                INSERT INTO radio_snapshots(
                    snapshot_id, interface_name, ssid, channel, frequency, tx_power,
                    noise, htmode, up, client_count, avg_signal, total_airtime,
                    rx_bytes, tx_bytes, rx_packets, tx_packets, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    radio.interface_name,
                    radio.ssid,
                    radio.channel,
                    radio.frequency,
                    radio.tx_power,
                    radio.noise,
                    radio.htmode,
                    int(radio.up),
                    radio.client_count,
                    radio.avg_signal,
                    radio.total_airtime,
                    radio.rx_bytes,
                    radio.tx_bytes,
                    radio.rx_packets,
                    radio.tx_packets,
                    json.dumps(radio.raw),
                ),
            )
            radio_snapshot_id = radio_cursor.lastrowid
            for client in radio.clients:
                self.connection.execute(
                    """
                    INSERT INTO station_snapshots(
                        radio_snapshot_id, station_mac, vendor, signal, rx_rate, tx_rate,
                        rx_bytes, tx_bytes, rx_packets, tx_packets, airtime_rx, airtime_tx, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        radio_snapshot_id,
                        client.get("mac", ""),
                        client.get("vendor", ""),
                        client.get("signal"),
                        int(client.get("rate", {}).get("rx", 0) or 0),
                        int(client.get("rate", {}).get("tx", 0) or 0),
                        int(client.get("bytes", {}).get("rx", 0) or 0),
                        int(client.get("bytes", {}).get("tx", 0) or 0),
                        int(client.get("packets", {}).get("rx", 0) or 0),
                        int(client.get("packets", {}).get("tx", 0) or 0),
                        int(client.get("airtime", {}).get("rx", 0) or 0),
                        int(client.get("airtime", {}).get("tx", 0) or 0),
                        json.dumps(client),
                    ),
                )
        self.connection.commit()

    def insert_action(self, timestamp: str, action: ControlAction, status: str, response: dict | str | None) -> None:
        """Store a balancing action and its API response (or dry-run marker)."""
        self.connection.execute(
            """
            INSERT INTO control_actions(
                timestamp, ap_id, ap_name, interface_name, band, action_type,
                old_value, new_value, command_text, reason, status, response_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                action.ap.device_id,
                action.ap.name,
                action.interface_name,
                action.band,
                action.action_type,
                action.old_value,
                action.new_value,
                action.command,
                action.reason,
                status,
                json.dumps(response) if response is not None else None,
            ),
        )
        self.connection.commit()

    def upsert_wifi_sessions(self, sessions: list[dict]) -> None:
        """Insert or update Wi-Fi session records fetched from the OpenWISP API."""
        for session in sessions:
            client = session.get("client", {})
            self.connection.execute(
                """
                INSERT INTO wifi_sessions(
                    id, device_name, interface_name, ssid, station_mac, vendor,
                    start_time, stop_time, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    device_name=excluded.device_name,
                    interface_name=excluded.interface_name,
                    ssid=excluded.ssid,
                    station_mac=excluded.station_mac,
                    vendor=excluded.vendor,
                    start_time=excluded.start_time,
                    stop_time=excluded.stop_time,
                    raw_json=excluded.raw_json
                """,
                (
                    session.get("id"),
                    session.get("device", ""),
                    session.get("interface_name", ""),
                    session.get("ssid", ""),
                    client.get("mac_address", ""),
                    client.get("vendor", ""),
                    session.get("start_time"),
                    session.get("stop_time"),
                    json.dumps(session),
                ),
            )
        self.connection.commit()

    def export_radio_series_csv(self, output_path: Path, interface_name: str | None = None) -> None:
        """Write a CSV time-series of radio metrics, optionally filtered by interface name.

        Columns: timestamp, ap_name, interface_name, channel, tx_power,
                 client_count, avg_signal, total_airtime, rx_bytes, tx_bytes.
        """
        query = """
        SELECT ps.timestamp, ps.ap_name, rs.interface_name, rs.channel, rs.tx_power,
               rs.client_count, rs.avg_signal, rs.total_airtime, rs.rx_bytes, rs.tx_bytes
        FROM radio_snapshots rs
        JOIN poll_snapshots ps ON ps.id = rs.snapshot_id
        """
        params: tuple = ()
        if interface_name:
            query += " WHERE rs.interface_name = ?"
            params = (interface_name,)
        query += " ORDER BY ps.timestamp, ps.ap_name"

        rows = self.connection.execute(query, params).fetchall()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "timestamp",
                    "ap_name",
                    "interface_name",
                    "channel",
                    "tx_power",
                    "client_count",
                    "avg_signal",
                    "total_airtime",
                    "rx_bytes",
                    "tx_bytes",
                ]
            )
            writer.writerows([tuple(row) for row in rows])
