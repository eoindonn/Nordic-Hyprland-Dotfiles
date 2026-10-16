from fabric import Application
from fabric.utils import get_relative_path
from gi.repository import Gdk
from loguru import logger

from .bar import StatusBar
from .system import Services
from .widgets.notifications import NotificationPopups


class BarManager:
    """Keeps exactly one bar on every connected monitor, including hotplugged ones."""

    def __init__(self, app: Application):
        self.app = app
        self.services = Services()
        self.bars: dict[Gdk.Monitor, StatusBar] = {}
        # hotplugged monitors still waiting for their geometry -> handler id
        self.pending: dict[Gdk.Monitor, int] = {}
        # notification popups appear on one monitor only
        self.popups: NotificationPopups | None = None
        self.popups_monitor: Gdk.Monitor | None = None

        display = Gdk.Display.get_default()
        for i in range(display.get_n_monitors()):
            self.add_bar(display.get_monitor(i))

        display.connect("monitor-added", lambda _, monitor: self.on_monitor_added(monitor))
        display.connect("monitor-removed", lambda _, monitor: self.remove_bar(monitor))

    def on_monitor_added(self, monitor: Gdk.Monitor):
        # a hotplugged monitor is announced before the compositor has sent its
        # details; placing a layer surface on it then lands the bar elsewhere
        if monitor.get_geometry().width > 0:
            return self.add_bar(monitor)

        def on_geometry(*_):
            if monitor.get_geometry().width == 0:
                return
            monitor.disconnect(self.pending.pop(monitor))
            self.add_bar(monitor)

        self.pending[monitor] = monitor.connect("notify::geometry", on_geometry)

    def add_bar(self, monitor: Gdk.Monitor):
        if monitor in self.bars:
            return
        logger.info(f"adding bar on {self.describe(monitor)}")
        bar = StatusBar(monitor=monitor, services=self.services)
        self.bars[monitor] = bar
        self.app.add_window(bar)
        self.ensure_popups()

    def remove_bar(self, monitor: Gdk.Monitor):
        if (handler := self.pending.pop(monitor, None)) is not None:
            monitor.disconnect(handler)
        if (bar := self.bars.pop(monitor, None)) is None:
            return
        logger.info(f"removing bar from {self.describe(monitor)}")
        self.app.remove_window(bar)
        bar.destroy()
        if monitor == self.popups_monitor:
            self.popups.destroy()
            self.popups = self.popups_monitor = None
            self.ensure_popups()

    def ensure_popups(self):
        if self.popups is None and self.bars:
            monitor, bar = next(iter(self.bars.items()))
            self.popups = NotificationPopups(monitor, bar, self.services.notifications)
            self.popups_monitor = monitor
            self.app.add_window(self.popups)

    @staticmethod
    def describe(monitor: Gdk.Monitor) -> str:
        geometry = monitor.get_geometry()
        return f"{monitor.get_model() or 'monitor'} ({geometry.width}x{geometry.height}+{geometry.x}+{geometry.y})"


def main():
    app = Application("bar")
    app.set_stylesheet_from_file(get_relative_path("./style.css"))
    BarManager(app)
    app.run()


if __name__ == "__main__":
    main()
