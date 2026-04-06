"""Snapshot normalisation and RSSI-based load-balancing controller.

The controller compares the number of active clients on two APs sharing the
same band and disconnects the worst-RSSI clients from the more-loaded AP so
that stations can reassociate to the less-loaded one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .config import APConfig, AppConfig


@dataclass
class RadioState:
    """Normalised snapshot of a single wireless interface."""

    ap_id: str
    ap_name: str
    interface_name: str
    ssid: str
    channel: Optional[int]
    frequency: Optional[int]
    tx_power: Optional[int]
    noise: Optional[int]
    htmode: str
    up: bool
    client_count: int
    avg_signal: Optional[float]
    total_airtime: int
    rx_bytes: int
    tx_bytes: int
    rx_packets: int
    tx_packets: int
    clients: list[dict[str, Any]]
    raw: dict[str, Any]


@dataclass
class ControlAction:
    """A balancing action to be applied (or logged in dry-run mode)."""

    ap: APConfig
    interface_name: str
    band: str
    action_type: str
    old_value: int
    new_value: int
    macs_to_disconnect: list[str]
    command: str
    reason: str


def normalize_snapshot(raw_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw OpenWISP monitoring snapshot into a structured dict.

    Returns a dict with keys:
      - ``ap_id``    : device UUID
      - ``ap_name``  : device display name
      - ``radios``   : list of :class:`RadioState` objects (wireless interfaces only)
      - ``local_time``: AP local epoch timestamp, or None
      - ``raw``      : the original snapshot dict
    """
    ap_id   = raw_snapshot.get("id", "")
    ap_name = raw_snapshot.get("name", "")
    local_time: Optional[int] = raw_snapshot.get("data", {}).get("general", {}).get("local_time")
    radios  = []
    for interface in raw_snapshot.get("data", {}).get("interfaces", []):
        if interface.get("type") != "wireless":
            continue
        wireless = interface.get("wireless", {})
        clients  = wireless.get("clients", [])
        # Average signal over clients that report a valid RSSI value.
        signals  = [c.get("signal") for c in clients if isinstance(c.get("signal"), (int, float))]
        total_airtime = sum(
            int(c.get("airtime", {}).get("rx", 0)) + int(c.get("airtime", {}).get("tx", 0))
            for c in clients
        )
        stats = interface.get("statistics", {})
        radios.append(RadioState(
            ap_id=ap_id,
            ap_name=ap_name,
            interface_name=interface.get("name", ""),
            ssid=wireless.get("ssid", ""),
            channel=wireless.get("channel"),
            frequency=wireless.get("frequency"),
            tx_power=wireless.get("tx_power"),
            noise=wireless.get("noise"),
            htmode=wireless.get("htmode", ""),
            up=bool(interface.get("up", False)),
            client_count=len(clients),
            avg_signal=(sum(signals) / len(signals)) if signals else None,
            total_airtime=total_airtime,
            rx_bytes=int(stats.get("rx_bytes", 0) or 0),
            tx_bytes=int(stats.get("tx_bytes", 0) or 0),
            rx_packets=int(stats.get("rx_packets", 0) or 0),
            tx_packets=int(stats.get("tx_packets", 0) or 0),
            clients=clients,
            raw=interface,
        ))
    return {"ap_id": ap_id, "ap_name": ap_name, "radios": radios, "local_time": local_time, "raw": raw_snapshot}


def get_radio_map(normalized_snapshot: dict[str, Any], ap: APConfig) -> dict[str, RadioState]:
    """Return a band -> RadioState mapping for a single AP snapshot.

    Only bands whose interface name is present in the snapshot are included.
    """
    iface_to_state = {r.interface_name: r for r in normalized_snapshot["radios"]}
    return {
        rc.band: iface_to_state[rc.interface_name]
        for rc in ap.radios
        if rc.interface_name in iface_to_state
    }


def _active_clients(radio: RadioState) -> list[dict[str, Any]]:
    """Return genuinely associated clients: those with a non-None signal value.

    hostapd sometimes keeps stale entries (no signal) after a station leaves;
    this function filters them out so the count reflects the real load.
    """
    return [c for c in radio.clients if c.get("mac") and c.get("signal") is not None]


def build_disassociate_command(interface: str, macs: list[str]) -> str:
    """Build a shell command that disassociates a list of stations via hostapd_cli.

    Returns 'true' (a no-op) when *macs* is empty.
    """
    if not macs:
        return "true"
    return " ; ".join(f"hostapd_cli -i {interface} disassociate {mac}" for mac in macs)


class RSSIBalancingController:
    """
    RSSI-based inter-AP load-balancing controller.

    Algorithm (simplified pseudo-code)::

        while True:
            sleep(poll_interval)
            c1 = active clients on AP1  (signal != None)
            c2 = active clients on AP2
            diff = |len(c1) - len(c2)|

            if diff < client_diff_threshold:
                log "balanced"
                continue

            overloaded_ap = AP1 if len(c1) > len(c2) else AP2
            n_kick = diff // 2
            sort clients on overloaded_ap by ascending RSSI (worst first)

            for i in range(n_kick):
                disassociate clients[i] from overloaded_ap

            sleep(control_cooldown)  # wait for stations to roam before re-evaluating
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.last_skip_reason: str = ""

    def decide(self, snapshots: dict[str, dict]) -> Optional[ControlAction]:
        """Analyse the latest snapshots and return a :class:`ControlAction`, or None.

        Returns None (and sets :attr:`last_skip_reason`) when the network is
        already considered balanced or no common band is available.
        """
        ap1, ap2 = self.config.aps
        rm1 = get_radio_map(snapshots[ap1.name], ap1)
        rm2 = get_radio_map(snapshots[ap2.name], ap2)

        common_bands = sorted(set(rm1) & set(rm2))
        if not common_bands:
            self.last_skip_reason = "no common band between AP1 and AP2"
            return None
        band = common_bands[0]

        c1 = _active_clients(rm1[band])
        c2 = _active_clients(rm2[band])
        n1, n2 = len(c1), len(c2)
        diff = abs(n1 - n2)

        if diff < self.config.client_diff_threshold:
            self.last_skip_reason = (
                f"AP1={n1} AP2={n2} diff={diff} (< {self.config.client_diff_threshold}) → balanced"
            )
            return None

        # Select the more-loaded AP.
        if n1 > n2:
            overloaded_ap  = ap1
            overloaded_iface = rm1[band].interface_name
            pool = list(c1)
            n_over, n_under = n1, n2
        else:
            overloaded_ap  = ap2
            overloaded_iface = rm2[band].interface_name
            pool = list(c2)
            n_over, n_under = n2, n1

        # Sort by ascending RSSI: weakest signal first (most likely to roam successfully).
        pool.sort(key=lambda c: int(c["signal"]))

        # Number of clients to kick to restore balance.
        n_kick = diff // 2
        targets = pool[:n_kick]
        macs = [c["mac"] for c in targets]
        info = ", ".join(f"{c['mac']}({c['signal']}dBm)" for c in targets)

        return ControlAction(
            ap=overloaded_ap,
            interface_name=overloaded_iface,
            band=band,
            action_type="disassociate",
            old_value=n_over,
            new_value=n_over - n_kick,
            macs_to_disconnect=macs,
            command=build_disassociate_command(overloaded_iface, macs),
            reason=(
                f"[{band}] {overloaded_ap.name}={n_over} vs other={n_under} "
                f"(diff={diff}) -> kick {n_kick}: {info}"
            ),
        )
