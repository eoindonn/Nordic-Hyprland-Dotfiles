from fabric.hyprland.widgets import (
    HyprlandActiveWindow,
    HyprlandWorkspaces,
    WorkspaceButton,
)
from fabric.widgets.box import Box
from fabric.widgets.centerbox import CenterBox
from fabric.widgets.datetime import DateTime
from fabric.widgets.wayland import WaylandWindow as Window
from gi.repository import Gdk

from .system import Services
from .utils import pin_to_monitor
from .widgets.backups import BackupIndicator
from .widgets.calendar import CalendarPanel
from .widgets.clipboard import ClipboardButton, ClipboardHistory
from .widgets.deck import DeckButton, DeckPanel
from .widgets.notifications import NotificationButton, NotificationHub
from .widgets.dropdown import Dropdown, DropdownHost
from .widgets.shell import draw_bar, shell_style
from .widgets.quick_settings.panel import QuickSettings
from .widgets.system_status import BatteryRing, StatRing
from .widgets.tray import FilteredSystemTray
from .widgets.volume import VolumeWidget


class StatusBar(Window):
    def __init__(self, monitor: Gdk.Monitor, services: Services):
        stats = services.stats
        super().__init__(
            name="bar",
            layer="top",
            anchor="left top right",
            margin="10px 10px -2px 10px",
            exclusivity="auto",
            visible=False,
        )
        pin_to_monitor(self, monitor)

        # the dropdowns share one layer surface below the bar, are drawn as
        # part of the bar, and are torn down with it
        self.dropdown_host = DropdownHost(monitor, self)
        self.clipboard_dropdown = Dropdown(
            self.dropdown_host, lambda close: ClipboardHistory(close, services.clipboard), width=400, align="left"
        )
        self.dropdown = Dropdown(
            self.dropdown_host, lambda close: QuickSettings(services, close), width=380
        )
        self.calendar_dropdown = Dropdown(
            self.dropdown_host, lambda close: CalendarPanel(close, services.calendar), width=360, align="right"
        )
        self.deck_dropdown = Dropdown(
            self.dropdown_host, lambda close: DeckPanel(close, services.deck), width=440, align="left",
            surface="shell-surface-solid",
        )
        self.notification_dropdown = Dropdown(
            self.dropdown_host, lambda close: NotificationHub(close, services), width=400, align="right"
        )
        self.dropdowns = [
            self.clipboard_dropdown,
            self.deck_dropdown,
            self.dropdown,
            self.calendar_dropdown,
            self.notification_dropdown,
        ]
        self.connect("destroy", lambda *_: self.destroy_popups())

        active_window = HyprlandActiveWindow(name="hyprland-window")
        self.dropdown.attach(active_window)

        clipboard_button = ClipboardButton()
        deck_button = DeckButton()
        self.deck_dropdown.attach(deck_button)

        # one format only: Fabric's DateTime cycles formats on click, and a
        # click here opens the calendar instead
        notification_button = NotificationButton(services)
        self.notification_dropdown.attach(notification_button)

        clock = DateTime(name="date-time", formatters=("%I:%M %p",))
        clock.set_tooltip_text("Calendar")
        self.calendar_dropdown.attach(clock)
        self.clipboard_dropdown.attach(clipboard_button)

        status_children = [
            BackupIndicator(services.backups),
            StatRing(stats.ram, "indicator-sensors-memory", 18, name="ram-progress-bar"),
            StatRing(stats.cpu, "cpu-symbolic", 16, name="cpu-progress-bar"),
        ]
        if stats.battery is not None:
            status_children.append(BatteryRing(stats.battery))
        status_children.append(VolumeWidget(stats))

        self.inner = CenterBox(
            name="bar-inner",
            start_children=Box(
                name="start-container",
                spacing=4,
                children=[
                    HyprlandWorkspaces(
                        name="workspaces",
                        spacing=4,
                        buttons_factory=lambda ws_id: WorkspaceButton(id=ws_id, label=None),
                    ),
                    clipboard_button,
                    deck_button,
                ],
            ),
            center_children=Box(
                name="center-container",
                children=active_window,
            ),
            end_children=Box(
                name="end-container",
                spacing=4,
                orientation="h",
                children=[
                    Box(
                        name="system-status",
                        spacing=4,
                        orientation="h",
                        children=status_children,
                    ),
                    FilteredSystemTray(name="system-tray", spacing=4),
                    notification_button,
                    clock,
                ],
            ),
        )

        # the bar's background and border are drawn by hand so they can open
        # up where a dropdown attaches (see widgets/shell.py)
        self.inner.connect("draw", self.draw_shape)
        self.children = self.inner
        self.show_all()

    def draw_shape(self, widget, cr):
        sheets = [
            (sheet, dropdown.bar_offset())
            for dropdown in self.dropdowns
            if (sheet := dropdown.current_sheet()) is not None
        ]
        alloc = widget.get_allocation()
        draw_bar(cr, alloc.width, alloc.height, shell_style(), sheets)
        return False

    def dropdown_opened(self, opened):
        for dropdown in self.dropdowns:
            if dropdown is not opened:
                dropdown.dismiss()

    def redraw_shape(self):
        self.inner.queue_draw()

    def destroy_popups(self):
        self.dropdown_host.destroy()
