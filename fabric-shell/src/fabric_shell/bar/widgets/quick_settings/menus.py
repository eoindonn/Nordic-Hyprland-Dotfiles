from collections.abc import Callable
from pathlib import Path

from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.image import Image
from fabric.widgets.label import Label
from fabric.widgets.revealer import Revealer
from gi.repository import Gtk

from ...system import Services
from ...system.nextcloud import DESCRIPTIONS as SYNC_DESCRIPTIONS
from ...utils import run_async
from ..power import LOG_OUT, POWER_ACTIONS, power_buttons

MAX_NETWORKS = 8


def menu_item(
    icon_name: str,
    text: str,
    on_clicked: Callable[[], None] | None = None,
    trailing: Gtk.Widget | None = None,
    active: bool = False,
    subtitle: str | None = None,
) -> Button:
    title = Label(label=text, h_align="start", ellipsization="end", max_chars_width=24)
    if subtitle:
        title = Box(
            orientation="v",
            v_align="center",
            children=[title, Label(name="menu-item-subtitle", label=subtitle, h_align="start")],
        )
    title.set_hexpand(True)
    children = [Image(icon_name=icon_name, icon_size=16), title]
    if trailing is not None:
        children.append(trailing)
    button = Button(
        name="menu-item",
        child=Box(spacing=10, children=children),
        on_clicked=lambda *_: on_clicked() if on_clicked else None,
    )
    if active:
        button.add_style_class("active")
    return button


class Collapsible(Revealer):
    """Revealer that is fully hidden when collapsed, so it adds no spacing."""

    def __init__(self, child: Gtk.Widget):
        super().__init__(child=child, transition_type="slide-down", transition_duration=260)
        self.connect("notify::child-revealed", lambda *_: self._sync_visible())
        self.set_no_show_all(True)
        self.hide()

    def _sync_visible(self):
        self.set_visible(self.get_reveal_child() or self.get_child_revealed())

    @property
    def is_open(self) -> bool:
        return self.get_reveal_child()

    def set_open(self, open_: bool):
        if open_:
            self.show()
        self.set_reveal_child(open_)
        self._sync_visible()


class DetailMenu(Collapsible):
    """Slide-down list shown under a tile, like GNOME's quick settings menus."""

    def __init__(
        self,
        icon_name: str,
        title: str,
        close_panel: Callable[[], None],
        settings: tuple[str, list[str]] | None = None,
    ):
        self.close_panel = close_panel
        self.items = Box(orientation="v", spacing=2)
        children = [
            Box(
                name="menu-header",
                spacing=10,
                children=[
                    Image(icon_name=icon_name, icon_size=20),
                    Label(name="menu-title", label=title),
                ],
            ),
            self.items,
        ]
        if settings is not None:
            label, command = settings
            children.append(
                Button(
                    name="menu-settings",
                    label=label,
                    on_clicked=lambda *_: self.launch(command),
                )
            )
        super().__init__(Box(name="detail-menu", orientation="v", spacing=6, children=children))

    def set_open(self, open_: bool):
        if open_:
            self.rebuild()
        super().set_open(open_)

    def launch(self, command: list[str]):
        run_async(command, capture=False)
        self.close_panel()

    def set_items(self, widgets: list[Gtk.Widget]):
        for child in self.items.children:
            child.destroy()
        for widget in widgets:
            self.items.add(widget)
        self.items.show_all()

    def rebuild(self):
        """Fill `items`; called on open and when the backing service changes."""


class WifiMenu(DetailMenu):
    def __init__(self, services: Services, close_panel):
        self.network = services.network
        super().__init__(
            "network-wireless-symbolic",
            "Wi-Fi",
            close_panel,
            settings=("Network settings", ["nm-connection-editor"]),
        )

    def set_open(self, open_: bool):
        if open_:
            self.network.scan()
        super().set_open(open_)

    def rebuild(self):
        if not self.network.enabled:
            return self.set_items([Label(name="menu-empty", label="Wi-Fi is off")])

        items = []
        for ap in self.network.access_points()[:MAX_NETWORKS]:
            if ap.active:
                trailing = Image(icon_name="object-select-symbolic", icon_size=14)
            elif ap.secured:
                trailing = Image(icon_name="changes-prevent-symbolic", icon_size=14)
            else:
                trailing = None
            items.append(
                menu_item(
                    ap.icon_name,
                    ap.ssid,
                    on_clicked=None if ap.active else lambda ssid=ap.ssid: self.connect_to(ssid),
                    trailing=trailing,
                    active=ap.active,
                )
            )
        self.set_items(items or [Label(name="menu-empty", label="No networks found")])

    def connect_to(self, ssid: str):
        def on_failure():
            # without a saved profile or an agent to ask, nmcli can't get the password
            run_async(
                [
                    "notify-send",
                    "-a", "Network",
                    "-i", "network-wireless-symbolic",
                    f"Couldn't connect to {ssid}",
                    "If it needs a password, add it in Network settings.",
                ],
                capture=False,
            )

        self.network.connect_to(ssid, on_failure=on_failure)


class BluetoothMenu(DetailMenu):
    def __init__(self, services: Services, close_panel):
        self.bluetooth = services.bluetooth
        super().__init__(
            "bluetooth-active-symbolic",
            "Bluetooth",
            close_panel,
            settings=("Bluetooth settings", ["overskride"]),
        )

    def rebuild(self):
        if not self.bluetooth.powered:
            return self.set_items([Label(name="menu-empty", label="Bluetooth is off")])

        items = []
        for device in self.bluetooth.devices():
            status = "Connecting…" if device.connecting else "Connected" if device.connected else ""
            items.append(
                menu_item(
                    device.icon_name,
                    device.name,
                    on_clicked=lambda path=device.path: self.bluetooth.toggle_device(path),
                    trailing=Label(name="menu-status", label=status) if status else None,
                    active=device.connected,
                )
            )
        self.set_items(items or [Label(name="menu-empty", label="No paired devices")])


class PowerMenu(Collapsible):
    def __init__(self):
        self.buttons = power_buttons(show_text=True, actions=[*POWER_ACTIONS, LOG_OUT])
        super().__init__(Box(name="detail-menu", orientation="v", spacing=2, children=self.buttons))

    def set_open(self, open_: bool):
        if not open_:
            for button in self.buttons:
                button.disarm()
        super().set_open(open_)


class AudioDeviceMenu(DetailMenu):
    """Pick the default output ("speaker") or input ("microphone") device."""

    def __init__(self, services: Services, kind: str, close_panel):
        self.audio = services.audio
        self.kind = kind
        is_output = kind == "speaker"
        self._pactl_command = "set-default-sink" if is_output else "set-default-source"
        self._fallback_icon = "audio-speakers-symbolic" if is_output else "audio-input-microphone-symbolic"
        super().__init__(
            "audio-speakers-symbolic" if is_output else "audio-input-microphone-symbolic",
            "Sound Output" if is_output else "Sound Input",
            close_panel,
            settings=("Sound settings", ["pavucontrol"]),
        )

    def rebuild(self):
        if self.audio is None:
            return self.set_items([])
        devices = self.audio.speakers if self.kind == "speaker" else self.audio.microphones
        current = getattr(self.audio, self.kind)
        items = []
        for device in devices:
            is_default = current is not None and device.id == current.id
            items.append(
                menu_item(
                    self.device_icon(device.icon_name),
                    device.description or device.name,
                    on_clicked=None if is_default else lambda name=device.name: self.select(name),
                    trailing=Image(icon_name="object-select-symbolic", icon_size=14) if is_default else None,
                    active=is_default,
                )
            )
        self.set_items(items or [Label(name="menu-empty", label="No devices")])

    def device_icon(self, icon_name: str | None) -> str:
        # PipeWire reports names like "audio-card-analog-pci" that many
        # icon themes lack; fall back rather than showing a broken icon
        if icon_name and Gtk.IconTheme.get_default().has_icon(icon_name):
            return icon_name
        return self._fallback_icon

    def select(self, name: str):
        # libcvc picks the change up and the Audio service re-emits it
        run_async(["pactl", self._pactl_command, name], capture=False)


SYNC_ICONS = {
    "synced": "emblem-ok-symbolic",
    "syncing": "emblem-synchronizing-symbolic",
    "warning": "dialog-warning-symbolic",
    "error": "dialog-error-symbolic",
}
FOLDER_ICONS = {
    "Documents": "folder-documents-symbolic",
    "Pictures": "folder-pictures-symbolic",
    "Desktop": "user-desktop-symbolic",
}


class NextcloudMenu(DetailMenu):
    """Per-folder sync status; clicking a folder opens it."""

    def __init__(self, services: Services, close_panel):
        self.sync = services.nextcloud
        super().__init__(
            "nextcloud-symbolic",
            "Nextcloud",
            close_panel,
            settings=("Open Nextcloud", ["nextcloud"]),
        )

    def rebuild(self):
        if not self.sync.connected:
            return self.set_items([Label(name="menu-empty", label="Nextcloud isn't running")])

        items = []
        for folder, state in sorted(self.sync.folders.items()):
            name = Path(folder).name
            items.append(
                menu_item(
                    FOLDER_ICONS.get(name, "folder-symbolic"),
                    name,
                    on_clicked=lambda folder=folder: self.launch(["xdg-open", folder]),
                    trailing=Image(
                        name=f"sync-{state}",
                        icon_name=SYNC_ICONS[state],
                        icon_size=14,
                        tooltip_text=SYNC_DESCRIPTIONS[state],
                    ),
                )
            )
        self.set_items(items or [Label(name="menu-empty", label="Offline: no folders are syncing")])


class TailscaleMenu(DetailMenu):
    """Other machines on the tailnet; clicking one copies its IP."""

    def __init__(self, services: Services, close_panel):
        self.tailscale = services.tailscale
        super().__init__(
            "network-workgroup-symbolic",
            "Tailscale",
            close_panel,
            settings=("Admin console", ["xdg-open", "https://login.tailscale.com/admin/machines"]),
        )

    def rebuild(self):
        ts = self.tailscale
        if not ts.running:
            message = "Signed out" if ts.state == "NeedsLogin" else "Tailscale is off"
            return self.set_items([Label(name="menu-empty", label=message)])

        items = [
            Label(
                name="menu-subtitle",
                label=f"This device: {ts.hostname} · {ts.ip}",
                h_align="start",
                ellipsization="end",
            )
        ]
        for peer in ts.peers:
            row = menu_item(
                peer.icon_name,
                peer.name,
                on_clicked=lambda peer=peer: self.copy_address(peer),
                trailing=Label(name="menu-status", label=peer.status),
                active=peer.online,
                subtitle=peer.ip,
            )
            tooltip = [peer.dns_name]
            if peer.routes:
                tooltip.append("Routes: " + ", ".join(peer.routes))
            row.set_tooltip_text("\n".join([*tooltip, "Click to copy the IP address"]))
            if not peer.online:
                row.add_style_class("offline")
            items.append(row)
        if not ts.peers:
            items.append(Label(name="menu-empty", label="No other devices"))
        self.set_items(items)

    def copy_address(self, peer):
        run_async(["wl-copy", peer.ip], capture=False)
        self.close_panel()
