from dataclasses import dataclass

import gi

gi.require_version("NM", "1.0")
from fabric.core.service import Service, Signal
from gi.repository import GLib, NM
from loguru import logger

from ..utils import run_async

# the enum's name starts with a digit, so it can't be written as NM.80211ApFlags
AP_PRIVACY = getattr(NM, "80211ApFlags").PRIVACY


@dataclass
class AccessPoint:
    ssid: str
    strength: int
    secured: bool
    active: bool

    @property
    def icon_name(self) -> str:
        return wifi_icon(self.strength)


def wifi_icon(strength: int) -> str:
    for threshold, level in ((80, "excellent"), (55, "good"), (30, "ok"), (5, "weak")):
        if strength >= threshold:
            return f"network-wireless-signal-{level}-symbolic"
    return "network-wireless-signal-none-symbolic"


class NetworkService(Service):
    """Wi-Fi state from NetworkManager, updated by its signals rather than polling."""

    @Signal
    def changed(self) -> None: ...

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.client: NM.Client | None = None
        self.device: NM.DeviceWifi | None = None
        self._device_handlers: list[int] = []
        NM.Client.new_async(None, self._on_client_ready)

    def _on_client_ready(self, _, result):
        try:
            self.client = NM.Client.new_finish(result)
        except GLib.Error as e:
            logger.warning(f"NetworkManager unavailable: {e.message}")
            return
        for signal in ("notify::wireless-enabled", "notify::primary-connection", "notify::active-connections"):
            self.client.connect(signal, lambda *_: self.changed())
        self.client.connect("device-added", lambda *_: self._find_device())
        self.client.connect("device-removed", lambda *_: self._find_device())
        self._find_device()

    def _find_device(self):
        if self.device is not None:
            for handler in self._device_handlers:
                self.device.disconnect(handler)
        self._device_handlers = []

        self.device = next(
            (d for d in self.client.get_devices() if isinstance(d, NM.DeviceWifi)),
            None,
        )
        if self.device is not None:
            self._device_handlers = [
                self.device.connect(signal, lambda *_: self.changed())
                for signal in (
                    "notify::active-access-point",
                    "notify::state",
                    "access-point-added",
                    "access-point-removed",
                )
            ]
        self.changed()

    @property
    def available(self) -> bool:
        return self.device is not None

    @property
    def enabled(self) -> bool:
        return self.client is not None and self.client.wireless_get_enabled()

    def set_enabled(self, enabled: bool):
        run_async(["nmcli", "radio", "wifi", "on" if enabled else "off"], capture=False)

    @property
    def active_access_point(self) -> AccessPoint | None:
        if self.device is None or (ap := self.device.get_active_access_point()) is None:
            return None
        return self._to_access_point(ap, active=True)

    @property
    def connecting(self) -> bool:
        return self.device is not None and NM.DeviceState.PREPARE <= self.device.get_state() < NM.DeviceState.ACTIVATED

    def access_points(self) -> list[AccessPoint]:
        """Visible networks, one entry per SSID, connected first then by signal."""
        if self.device is None:
            return []
        active = self.device.get_active_access_point()
        best: dict[str, AccessPoint] = {}
        for ap in self.device.get_access_points():
            point = self._to_access_point(ap, active=ap == active)
            if not point.ssid:
                continue  # hidden network
            current = best.get(point.ssid)
            if current is None or point.active or (not current.active and point.strength > current.strength):
                best[point.ssid] = point
        return sorted(best.values(), key=lambda p: (not p.active, -p.strength))

    def scan(self):
        if self.device is None:
            return

        def on_done(device, result):
            try:
                device.request_scan_finish(result)
            except GLib.Error:
                pass  # NM refuses scans requested too soon after the last one

        self.device.request_scan_async(None, on_done)

    def connect_to(self, ssid: str, on_failure=None):
        # nmcli reuses a saved profile if one exists
        run_async(
            ["nmcli", "device", "wifi", "connect", ssid],
            capture=False,
            on_exit=lambda ok: None if ok or on_failure is None else on_failure(),
        )

    @staticmethod
    def _to_access_point(ap: NM.AccessPoint, active: bool) -> AccessPoint:
        ssid_bytes = ap.get_ssid()
        ssid = NM.utils_ssid_to_utf8(ssid_bytes.get_data()) if ssid_bytes else ""
        secured = bool(ap.get_wpa_flags() or ap.get_rsn_flags() or ap.get_flags() & AP_PRIVACY)
        return AccessPoint(ssid, ap.get_strength(), secured, active)
