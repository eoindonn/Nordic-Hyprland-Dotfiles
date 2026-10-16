import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone

from fabric.core.service import Service, Signal
from gi.repository import GLib
from loguru import logger

from ..utils import run_async

POLL_INTERVAL = 5000  # ms


@dataclass
class Peer:
    name: str
    dns_name: str
    ip: str
    os: str
    online: bool
    exit_node: bool  # currently routing our traffic through it
    last_seen: datetime | None
    routes: list[str]  # subnet routes it advertises (we accept these)

    @property
    def icon_name(self) -> str:
        if self.os in ("android", "iOS"):
            return "phone-symbolic"
        if self.os in ("freebsd", "openbsd"):
            return "network-server-symbolic"
        return "computer-symbolic"

    @property
    def status(self) -> str:
        if self.exit_node:
            return "Exit node"
        if self.online:
            return "Online · routes" if self.routes else "Online"
        return f"Seen {ago(self.last_seen)}" if self.last_seen else "Offline"


def ago(moment: datetime) -> str:
    seconds = (datetime.now(timezone.utc) - moment).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def parse_time(value: str | None) -> datetime | None:
    # tailscale reports the zero time ("0001-01-01...") for peers seen "now"
    if not value or value.startswith("0001"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class Tailscale(Service):
    """Tailscale state from `tailscale status --json`, polled asynchronously."""

    @Signal
    def changed(self) -> None: ...

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.available = shutil.which("tailscale") is not None
        self.state = "NoState"  # BackendState: Running, Stopped, NeedsLogin, ...
        self.hostname = ""
        self.ip = ""
        self.peers: list[Peer] = []
        self._busy = False
        if self.available:
            self.refresh()
            GLib.timeout_add(POLL_INTERVAL, lambda: (self.refresh(), True)[1])

    @property
    def running(self) -> bool:
        return self.state == "Running"

    @property
    def online_peers(self) -> list[Peer]:
        return [p for p in self.peers if p.online]

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        run_async(["tailscale", "status", "--json"], self._on_status)

    def _on_status(self, output: bytes):
        self._busy = False
        try:
            status = json.loads(output)
        except json.JSONDecodeError:
            # tailscaled not running
            return self._update("NoState", "", "", [])

        me = status.get("Self") or {}
        peers = [
            Peer(
                name=peer.get("HostName", "?"),
                dns_name=(peer.get("DNSName") or "").rstrip("."),
                ip=next(iter(peer.get("TailscaleIPs") or []), ""),
                os=peer.get("OS", ""),
                online=bool(peer.get("Online")),
                exit_node=bool(peer.get("ExitNode")),
                last_seen=parse_time(peer.get("LastSeen")),
                routes=list(peer.get("PrimaryRoutes") or []),
            )
            for peer in (status.get("Peer") or {}).values()
        ]
        peers.sort(key=lambda p: (not p.exit_node, not p.online, p.name.lower()))
        self._update(
            status.get("BackendState", "NoState"),
            me.get("HostName", ""),
            next(iter(me.get("TailscaleIPs") or []), ""),
            peers,
        )

    def _update(self, state, hostname, ip, peers):
        if (state, hostname, ip, peers) != (self.state, self.hostname, self.ip, self.peers):
            self.state, self.hostname, self.ip, self.peers = state, hostname, ip, peers
            self.changed()

    def set_running(self, running: bool):
        def done(ok: bool):
            if not ok:
                logger.warning(f"tailscale {'up' if running else 'down'} failed")
            self.refresh()

        if running:
            # `tailscale up --accept-routes` would demand every other
            # non-default flag be restated, so set the pref separately
            run_async(
                ["tailscale", "set", "--accept-routes=true"],
                capture=False,
                on_exit=lambda _: run_async(["tailscale", "up"], capture=False, on_exit=done),
            )
        else:
            run_async(["tailscale", "down"], capture=False, on_exit=done)
