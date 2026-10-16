import shutil

import gi

gi.require_version("NM", "1.0")
from fabric.core.service import Service, Signal
from gi.repository import GLib, NM
from loguru import logger

from ..utils import run_async
from .network import NetworkService


def notify(summary: str, body: str):
    run_async(
        ["notify-send", "-a", "Proton VPN", "-i", "network-vpn-symbolic", summary, body],
        capture=False,
    )

# how the Proton VPN app names the NetworkManager connections it creates
PROTON_PREFIX = "ProtonVPN"
PROTON_INTERFACE = "proton0"


# official CLI (package proton-vpn-cli); `connect` with no server picks the fastest
CLI = "protonvpn"


class ProtonVpn(Service):
    """Proton VPN, connected through the official CLI.

    Status comes from the NetworkManager connection Proton creates, so it
    stays right whichever client (CLI, GTK app) made the connection. Without
    the CLI installed, connecting falls back to opening the app.
    """

    @Signal
    def changed(self) -> None: ...

    def __init__(self, network: NetworkService, **kwargs):
        super().__init__(**kwargs)
        self.network = network
        self.has_cli = shutil.which(CLI) is not None
        self.available = self.has_cli or shutil.which("protonvpn-app") is not None
        self.active: NM.ActiveConnection | None = None
        self.pending = False  # CLI asked to connect, NM connection not up yet
        self._state_handler: int | None = None
        network.connect("changed", lambda *_: self._sync())
        self._sync()

    def _find_active(self) -> NM.ActiveConnection | None:
        if (client := self.network.client) is None:
            return None
        for connection in client.get_active_connections():
            if (connection.get_id() or "").startswith(PROTON_PREFIX) or any(
                device.get_iface() == PROTON_INTERFACE for device in connection.get_devices()
            ):
                return connection
        return None

    def _sync(self):
        active = self._find_active()
        if active is not None:
            self.pending = False
        if active is not self.active:
            if self.active is not None and self._state_handler is not None:
                self.active.disconnect(self._state_handler)
            self.active = active
            self._state_handler = (
                active.connect("notify::state", lambda *_: self.changed())
                if active is not None
                else None
            )
        self.changed()

    @property
    def connected(self) -> bool:
        return self.active is not None and self.active.get_state() == NM.ActiveConnectionState.ACTIVATED

    @property
    def connecting(self) -> bool:
        if self.active is None:
            return self.pending
        return self.active.get_state() == NM.ActiveConnectionState.ACTIVATING

    @property
    def server(self) -> str:
        if self.active is None:
            return ""
        return (self.active.get_id() or "").removeprefix(PROTON_PREFIX).strip()

    def open_app(self):
        # single-instance app: launching again just presents its window
        run_async(["protonvpn-app"], capture=False)

    def connect_fastest(self):
        if not self.has_cli:
            notify(
                "Proton VPN CLI not installed",
                "Install proton-vpn-cli to connect from the bar. Opening the app instead.",
            )
            return self.open_app()

        # the CLI refuses to run while the GUI app is open
        run_async(["pgrep", "-f", "protonvpn-app"], capture=False, on_exit=self._connect_unless_gui)

    def _connect_unless_gui(self, gui_running: bool):
        if gui_running:
            notify(
                "Close the Proton VPN app first",
                "Proton's command-line tool can't connect while the desktop app is running.",
            )
            return

        self.pending = True
        self.changed()
        output = []

        def done(ok: bool):
            self.pending = False
            self.changed()
            if not ok:
                lines = [l for l in b"".join(output).decode(errors="replace").splitlines() if l.strip()]
                notify("Proton VPN couldn't connect", lines[-1] if lines else f"Try `{CLI} connect` in a terminal.")

        run_async([CLI, "connect"], output.append, on_exit=done, merge_stderr=True)

    def deactivate(self):
        if self.has_cli:
            # let Proton tear down its own kill switch and routes
            run_async([CLI, "disconnect"], capture=False)
            return
        if self.active is None or (client := self.network.client) is None:
            return

        def on_done(client, result):
            try:
                client.deactivate_connection_finish(result)
            except GLib.Error as e:
                logger.warning(f"couldn't disconnect Proton VPN: {e.message}")

        client.deactivate_connection_async(self.active, None, on_done)
