from dataclasses import dataclass

from fabric.core.service import Service, Signal
from gi.repository import Gio, GLib
from loguru import logger

from ..utils import run_async

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"


@dataclass
class BluetoothDevice:
    path: str
    name: str
    icon_name: str
    connected: bool
    connecting: bool


def _prop(proxy: Gio.DBusProxy, name: str, default=None):
    value = proxy.get_cached_property(name)
    return value.unpack() if value is not None else default


class BluetoothService(Service):
    """BlueZ over D-Bus (Fabric's own service needs gnome-bluetooth)."""

    @Signal
    def changed(self) -> None: ...

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.manager: Gio.DBusObjectManagerClient | None = None
        self._connecting: set[str] = set()
        Gio.DBusObjectManagerClient.new_for_bus(
            Gio.BusType.SYSTEM,
            Gio.DBusObjectManagerClientFlags.NONE,
            BLUEZ,
            "/",
            None,
            None,
            None,
            self._on_manager_ready,
        )

    def _on_manager_ready(self, _, result):
        try:
            self.manager = Gio.DBusObjectManagerClient.new_for_bus_finish(result)
        except GLib.Error as e:
            logger.warning(f"BlueZ unavailable: {e.message}")
            return
        for signal in (
            "object-added",
            "object-removed",
            "interface-added",
            "interface-removed",
            "interface-proxy-properties-changed",
        ):
            self.manager.connect(signal, lambda *_: self.changed())
        self.changed()

    def _proxies(self, interface: str) -> list[Gio.DBusProxy]:
        if self.manager is None:
            return []
        return [
            proxy
            for obj in self.manager.get_objects()
            if (proxy := obj.get_interface(interface)) is not None
        ]

    @property
    def adapter(self) -> Gio.DBusProxy | None:
        return next(iter(self._proxies(ADAPTER)), None)

    @property
    def available(self) -> bool:
        return self.adapter is not None

    @property
    def powered(self) -> bool:
        return (adapter := self.adapter) is not None and _prop(adapter, "Powered", False)

    def set_powered(self, powered: bool, retry: bool = True):
        if (adapter := self.adapter) is None:
            return

        def on_done(proxy, result):
            try:
                proxy.call_finish(result)
            except GLib.Error as e:
                # BlueZ refuses to power on while rfkill blocks the radio
                if powered and retry:
                    run_async(
                        ["rfkill", "unblock", "bluetooth"],
                        capture=False,
                        on_exit=lambda _: self.set_powered(True, retry=False),
                    )
                else:
                    logger.warning(f"couldn't switch bluetooth {'on' if powered else 'off'}: {e.message}")

        adapter.call(
            "org.freedesktop.DBus.Properties.Set",
            GLib.Variant("(ssv)", (ADAPTER, "Powered", GLib.Variant("b", powered))),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            on_done,
        )

    def devices(self) -> list[BluetoothDevice]:
        """Paired devices, connected first."""
        devices = [
            BluetoothDevice(
                path=proxy.get_object_path(),
                name=_prop(proxy, "Alias") or _prop(proxy, "Name") or _prop(proxy, "Address", "?"),
                icon_name=f"{_prop(proxy, 'Icon') or 'bluetooth'}-symbolic",
                connected=_prop(proxy, "Connected", False),
                connecting=proxy.get_object_path() in self._connecting,
            )
            for proxy in self._proxies(DEVICE)
            if _prop(proxy, "Paired", False)
        ]
        return sorted(devices, key=lambda d: (not d.connected, d.name.lower()))

    def connected_devices(self) -> list[BluetoothDevice]:
        return [d for d in self.devices() if d.connected]

    def toggle_device(self, path: str):
        proxy = next((p for p in self._proxies(DEVICE) if p.get_object_path() == path), None)
        if proxy is None or path in self._connecting:
            return
        method = "Disconnect" if _prop(proxy, "Connected", False) else "Connect"
        self._connecting.add(path)
        self.changed()

        def on_done(proxy, result):
            try:
                proxy.call_finish(result)
            except GLib.Error as e:
                logger.warning(f"bluetooth {method.lower()} failed: {e.message}")
            self._connecting.discard(path)
            self.changed()

        proxy.call(method, None, Gio.DBusCallFlags.NONE, 30_000, None, on_done)
