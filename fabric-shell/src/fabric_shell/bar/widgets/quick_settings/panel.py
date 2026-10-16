from collections.abc import Callable

from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.image import Image
from fabric.widgets.label import Label

from ...services import bind
from ...system import Services
from ...system.network import wifi_icon
from ...system.nextcloud import DESCRIPTIONS as SYNC_DESCRIPTIONS
from ...utils import run_async
from .menus import AudioDeviceMenu, BluetoothMenu, NextcloudMenu, PowerMenu, TailscaleMenu, WifiMenu
from .sliders import SliderRow, mic_icon, volume_icon
from .tiles import QuickToggle


def tile_row(*tiles: QuickToggle) -> Box:
    # pad odd rows so a lone tile keeps half width, like GNOME's grid
    children = list(tiles) + ([Box()] if len(tiles) % 2 else [])
    row = Box(spacing=8, children=children)
    row.set_homogeneous(True)
    return row


class QuickSettings(Box):
    """GNOME-like control panel: header actions, sliders, toggle tiles."""

    def __init__(self, services: Services, close: Callable[[], None], **kwargs):
        self.services = services

        # header: battery on the left, lock and power on the right
        self.battery_label = Label(name="qs-battery")
        self.battery_label.set_no_show_all(True)
        self.power_menu = PowerMenu()
        header = Box(
            name="qs-header",
            spacing=6,
            children=[self.battery_label, Box(h_expand=True)],
        )
        header.add(
            Button(
                name="qs-header-button",
                image=Image(icon_name="system-lock-screen-symbolic", icon_size=16),
                tooltip_text="Lock",
                on_clicked=lambda *_: (close(), run_async(["hyprlock"], capture=False)),
            )
        )
        self.power_toggle = Button(
            name="qs-header-button",
            image=Image(icon_name="system-shutdown-symbolic", icon_size=16),
            tooltip_text="Power off / log out",
            on_clicked=lambda *_: self.toggle_menu(self.power_menu),
        )
        header.add(self.power_toggle)

        # sliders, each followed by its device menu
        self.output_menu = AudioDeviceMenu(services, "speaker", close)
        self.input_menu = AudioDeviceMenu(services, "microphone", close)
        self.volume = SliderRow(
            "audio-volume-high-symbolic",
            on_change=lambda level: self.set_level("speaker", level),
            on_icon_clicked=lambda: self.toggle_mute("speaker"),
            on_expand=lambda: self.toggle_menu(self.output_menu),
        )
        self.mic = SliderRow(
            "microphone-sensitivity-high-symbolic",
            on_change=lambda level: self.set_level("microphone", level),
            on_icon_clicked=lambda: self.toggle_mute("microphone"),
            on_expand=lambda: self.toggle_menu(self.input_menu),
        )
        self.brightness = SliderRow("display-brightness-symbolic", on_change=services.brightness.set_level)

        # tiles, each row followed by the menus its tiles open
        self.wifi_menu = WifiMenu(services, close)
        self.bluetooth_menu = BluetoothMenu(services, close)
        self.wifi = QuickToggle(
            "network-wireless-symbolic",
            "Wi-Fi",
            on_toggle=lambda: services.network.set_enabled(not services.network.enabled),
            on_expand=lambda: self.toggle_menu(self.wifi_menu),
        )
        self.bluetooth = QuickToggle(
            "bluetooth-active-symbolic",
            "Bluetooth",
            on_toggle=lambda: services.bluetooth.set_powered(not services.bluetooth.powered),
            on_expand=lambda: self.toggle_menu(self.bluetooth_menu),
        )
        self.vpn = QuickToggle("network-vpn-symbolic", "Proton VPN", on_toggle=lambda: self.toggle_vpn(close))
        self.dnd = QuickToggle(
            "notifications-disabled-symbolic",
            "Do Not Disturb",
            on_toggle=lambda: services.dnd.set_enabled(not services.dnd.enabled),
        )
        self.tailscale_menu = TailscaleMenu(services, close)
        self.tailscale = QuickToggle(
            "network-workgroup-symbolic",
            "Tailscale",
            on_toggle=lambda: services.tailscale.set_running(not services.tailscale.running),
            on_expand=lambda: self.toggle_menu(self.tailscale_menu),
        )

        self.nextcloud_menu = NextcloudMenu(services, close)
        self.nextcloud = QuickToggle(
            "nextcloud-symbolic",
            "Nextcloud",
            on_toggle=lambda: (close(), services.nextcloud.open_client()),
            on_expand=lambda: self.toggle_menu(self.nextcloud_menu),
        )

        # menu -> the widget that opens it (highlighted while open)
        self.menus = {
            self.power_menu: self.power_toggle,
            self.output_menu: self.volume,
            self.input_menu: self.mic,
            self.wifi_menu: self.wifi,
            self.bluetooth_menu: self.bluetooth,
            self.tailscale_menu: self.tailscale,
            self.nextcloud_menu: self.nextcloud,
        }

        super().__init__(
            name="quick-settings",
            orientation="v",
            spacing=10,
            v_align="start",
            children=[
                header,
                self.power_menu,
                self.volume,
                self.output_menu,
                self.mic,
                self.input_menu,
                self.brightness,
                tile_row(self.wifi, self.bluetooth),
                self.wifi_menu,
                self.bluetooth_menu,
                tile_row(self.vpn, self.dnd),
                tile_row(self.tailscale, self.nextcloud),
                self.tailscale_menu,
                self.nextcloud_menu,
            ],
            **kwargs,
        )

        self.connect_services()

    # -- wiring --------------------------------------------------------------

    def connect_services(self):
        s = self.services
        bind(self, s.network, "changed", lambda *_: self.sync_wifi())
        bind(self, s.vpn, "changed", lambda *_: self.sync_vpn())
        bind(self, s.bluetooth, "changed", lambda *_: self.sync_bluetooth())
        bind(self, s.dnd, "changed", lambda *_: self.sync_dnd())
        bind(self, s.tailscale, "changed", lambda *_: self.sync_tailscale())
        bind(self, s.brightness, "changed", lambda *_: self.sync_brightness())
        bind(self, s.nextcloud, "changed", lambda *_: self.sync_nextcloud())
        if s.stats.battery is not None:
            bind(self, s.stats.battery, "changed", lambda _, battery: self.sync_battery(battery))
            if s.stats.battery.value is not None:
                self.sync_battery(s.stats.battery.value)
        if s.audio is not None:
            bind(self, s.audio, "speaker-changed", lambda *_: self.sync_audio("speaker"))
            bind(self, s.audio, "microphone-changed", lambda *_: self.sync_audio("microphone"))
            bind(self, s.audio, "changed", lambda *_: self.sync_audio_menus())

        self.volume.set_visible(s.audio is not None)
        self.mic.set_visible(s.audio is not None)
        self.brightness.set_visible(s.brightness.available)
        self.vpn.set_visible(s.vpn.available)
        self.tailscale.set_visible(s.tailscale.available)
        for sync in (
            self.sync_wifi,
            self.sync_vpn,
            self.sync_bluetooth,
            self.sync_dnd,
            self.sync_tailscale,
            self.sync_brightness,
            self.sync_nextcloud,
        ):
            sync()
        self.sync_audio("speaker")
        self.sync_audio("microphone")

    def on_open(self):
        # the backlight has no change notifications, so re-read it (a
        # slider value change only redraws; it doesn't re-lay-out the panel)
        self.services.brightness.refresh()

    def on_close(self):
        for menu in self.menus:
            self.set_menu_open(menu, False)

    def toggle_menu(self, menu):
        opening = not menu.is_open
        for other in self.menus:
            self.set_menu_open(other, opening and other is menu)

    def set_menu_open(self, menu, open_: bool):
        menu.set_open(open_)
        trigger = self.menus[menu]
        if hasattr(trigger, "set_expanded"):
            trigger.set_expanded(open_)
        elif open_:
            trigger.add_style_class("active")
        else:
            trigger.remove_style_class("active")

    # -- actions -------------------------------------------------------------

    def _stream(self, kind: str):
        audio = self.services.audio
        return getattr(audio, kind) if audio is not None else None

    def set_level(self, kind: str, level: float):
        if (stream := self._stream(kind)) is not None:
            stream.volume = level * 100
            if stream.muted and level > 0:
                stream.muted = False

    def toggle_mute(self, kind: str):
        if (stream := self._stream(kind)) is not None:
            stream.muted = not stream.muted

    def toggle_vpn(self, close: Callable[[], None]):
        vpn = self.services.vpn
        if vpn.active is not None:
            vpn.deactivate()
        elif not vpn.pending:
            vpn.connect_fastest()

    # -- state -> widgets ----------------------------------------------------

    def sync_battery(self, battery):
        if battery is None:
            return
        charging = " (charging)" if battery.power_plugged and battery.percent < 99 else ""
        self.battery_label.set_label(f"{battery.percent:.0f}%{charging}")
        self.battery_label.show()

    def sync_audio(self, kind: str):
        if (stream := self._stream(kind)) is None:
            return
        level = stream.volume / 100
        row, icon = (self.volume, volume_icon) if kind == "speaker" else (self.mic, mic_icon)
        row.set_value(level, icon(level, stream.muted))
        if stream.muted:
            row.add_style_class("muted")
        else:
            row.remove_style_class("muted")
        self.sync_audio_menus()

    def sync_audio_menus(self):
        for menu in (self.output_menu, self.input_menu):
            if menu.is_open:
                menu.rebuild()

    def sync_brightness(self):
        self.brightness.set_value(self.services.brightness.level)

    def sync_wifi(self):
        network = self.services.network
        self.wifi.set_visible(network.available)
        if not network.enabled:
            self.wifi.update(False, "Off", "network-wireless-disabled-symbolic")
        elif (ap := network.active_access_point) is not None:
            self.wifi.update(True, ap.ssid, wifi_icon(ap.strength))
        elif network.connecting:
            self.wifi.update(True, "Connecting…", "network-wireless-acquiring-symbolic")
        else:
            self.wifi.update(True, "Not connected", "network-wireless-offline-symbolic")
        if self.wifi_menu.is_open:
            self.wifi_menu.rebuild()

    def sync_vpn(self):
        vpn = self.services.vpn
        if vpn.connected:
            self.vpn.update(True, vpn.server, "network-vpn-symbolic")
        elif vpn.connecting:
            self.vpn.update(True, "Connecting…", "network-vpn-acquiring-symbolic")
        else:
            self.vpn.update(False, "Off", "network-vpn-disabled-symbolic")

    def sync_bluetooth(self):
        bluetooth = self.services.bluetooth
        self.bluetooth.set_visible(bluetooth.available)
        if not bluetooth.powered:
            self.bluetooth.update(False, "Off", "bluetooth-disabled-symbolic")
        else:
            connected = bluetooth.connected_devices()
            subtitle = (
                connected[0].name if len(connected) == 1
                else f"{len(connected)} devices" if connected
                else ""
            )
            self.bluetooth.update(True, subtitle, "bluetooth-active-symbolic")
        if self.bluetooth_menu.is_open:
            self.bluetooth_menu.rebuild()

    def sync_dnd(self):
        self.dnd.update(self.services.dnd.enabled)

    def sync_tailscale(self):
        ts = self.services.tailscale
        if ts.running:
            online = len(ts.online_peers)
            self.tailscale.update(True, f"{online} online")
        elif ts.state == "NeedsLogin":
            self.tailscale.update(False, "Signed out")
        elif ts.state == "Starting":
            self.tailscale.update(True, "Connecting…")
        else:
            self.tailscale.update(False, "Off")
        if self.tailscale_menu.is_open:
            self.tailscale_menu.rebuild()

    def sync_nextcloud(self):
        state = self.services.nextcloud.state
        self.nextcloud.update(state != "stopped", SYNC_DESCRIPTIONS[state])
        for other in SYNC_DESCRIPTIONS:
            self.nextcloud.remove_style_class(other)
        self.nextcloud.add_style_class(state)
        if self.nextcloud_menu.is_open:
            self.nextcloud_menu.rebuild()
