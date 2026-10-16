import re
import time
from collections.abc import Callable
from pathlib import Path

import cairo
from fabric.notifications.service import Notification
from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.eventbox import EventBox
from fabric.widgets.image import Image
from fabric.widgets.label import Label
from fabric.widgets.revealer import Revealer
from fabric.widgets.scrolledwindow import ScrolledWindow
from fabric.widgets.wayland import WaylandWindow as Window
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

from ..services import bind
from ..system import Services
from ..system.notifications import URGENCY_CRITICAL, URGENCY_LOW, NotificationCenter
from ..utils import pin_to_monitor, run_async
from .images import fit, rounded_surface
from .media import MediaCard

# popup timeouts, as in the swaync config (seconds; 0 = stays until dismissed)
TIMEOUT = {URGENCY_LOW: 4, 1: 6, URGENCY_CRITICAL: 0}
MAX_POPUPS = 4
POPUP_WIDTH = 360
IMAGE_SIZE = 48
LIST_HEIGHT = 420

# markup notification bodies may use (body-markup); anything else is dropped
ALLOWED_TAGS = re.compile(r"</?(b|i|u|a)(\s[^>]*)?>", re.I)
ANY_TAG = re.compile(r"<[^>]+>")


def body_markup(body: str) -> str:
    """Notification body as safe Pango markup."""
    kept: list[str] = []

    def keep(match):
        kept.append(match.group(0))
        return f"{len(kept) - 1}"

    text = ALLOWED_TAGS.sub(keep, body)
    text = ANY_TAG.sub("", text)
    text = GLib.markup_escape_text(text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
    text = re.sub("(\\d+)", lambda m: kept[int(m.group(1))], text)
    try:
        # GTK labels handle <a> themselves; Pango's validator doesn't know it
        Pango.parse_markup(re.sub(r"<(/?)a\b[^>]*>", r"<\1span>", text), -1, "\0")
        return text
    except GLib.Error:
        # unbalanced tags etc.: fall back to plain text
        return GLib.markup_escape_text(ANY_TAG.sub("", body))


def time_ago(timestamp: float) -> str:
    seconds = max(0, time.time() - timestamp)
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return time.strftime("%-d %b", time.localtime(timestamp))


def app_icon(notification: Notification, size: int = 16) -> Image:
    icon = notification.app_icon or ""
    image = Image(name="notif-app-icon")
    path = GLib.filename_from_uri(icon)[0] if icon.startswith("file://") else icon
    if path.startswith("/") and Path(path).exists():
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
            image.set_from_pixbuf(pixbuf)
            return image
        except GLib.Error:
            pass
    theme = Gtk.IconTheme.get_default()
    name = icon if icon and theme.has_icon(icon) else (notification.app_name or "").lower()
    if not theme.has_icon(name):
        name = "dialog-information-symbolic"
    image.set_from_icon_name(name, Gtk.IconSize.MENU)
    image.set_pixel_size(size)
    return image


class NotificationCard(EventBox):
    """One notification: app, time, summary, body, image and actions.

    Clicking it runs the app's default action; × dismisses it for good.
    """

    def __init__(
        self,
        notification: Notification,
        center: NotificationCenter,
        popup: bool = False,
        on_close: Callable[[], None] | None = None,
        **kwargs,
    ):
        self.notification = notification
        self.center = center
        nid = notification.id

        close = Button(
            name="notif-close",
            image=Image(icon_name="window-close-symbolic", icon_size=12),
            tooltip_text="Dismiss",
            on_clicked=lambda *_: (on_close() if on_close else None, center.dismiss(nid)),
        )
        self.time_label = Label(name="notif-time", label=time_ago(notification.time))
        header = Box(
            spacing=6,
            children=[
                app_icon(notification),
                Label(name="notif-app", label=notification.app_name or "Notification", ellipsization="end", max_chars_width=26),
                Label(name="notif-time", label="·"),
                self.time_label,
                Box(h_expand=True),
                close,
            ],
        )

        text = Box(orientation="v", spacing=2, h_expand=True, v_align="center")
        if notification.summary:
            summary = Label(name="notif-summary", label=notification.summary, h_align="start", line_wrap="word-char")
            summary.set_xalign(0)
            summary.set_lines(2)
            summary.set_ellipsize(Pango.EllipsizeMode.END)
            text.add(summary)
        if notification.body:
            body = Label(name="notif-body", h_align="start", line_wrap="word-char")
            body.set_markup(body_markup(notification.body))
            body.set_xalign(0)
            body.set_lines(3 if popup else 5)
            body.set_ellipsize(Pango.EllipsizeMode.END)
            body.connect("activate-link", lambda _, uri: (run_async(["xdg-open", uri], capture=False), True)[1])
            text.add(body)

        content = Box(spacing=12, children=[text])
        if (pixbuf := self.image_pixbuf()) is not None:
            image = Image(name="notif-image", v_align="start")
            image.set_from_surface(rounded_surface(fit(pixbuf, IMAGE_SIZE, IMAGE_SIZE), 10))
            content.add(image)

        children = [header, content]
        actions = [a for a in notification.actions if a.identifier != "default"]
        if actions:
            row = Box(name="notif-actions", spacing=6)
            row.set_homogeneous(True)
            for action in actions:
                row.add(
                    Button(
                        name="notif-action",
                        label=action.label,
                        on_clicked=lambda *_, a=action.identifier: (on_close() if on_close else None, center.activate(nid, a)),
                    )
                )
            children.append(row)

        super().__init__(
            events=["button-press"],
            child=Box(name="notif-card", orientation="v", spacing=6, children=children),
            **kwargs,
        )
        self.get_child().add_style_class("popup" if popup else "in-hub")
        if notification.urgency == URGENCY_CRITICAL:
            self.get_child().add_style_class("critical")
        self.connect("button-press-event", self.on_press)
        self._on_close = on_close

    def image_pixbuf(self) -> GdkPixbuf.Pixbuf | None:
        try:
            return self.notification.image_pixbuf
        except Exception:
            return None

    def on_press(self, _, event: Gdk.EventButton):
        if event.button == Gdk.BUTTON_MIDDLE:
            self.center.dismiss(self.notification.id)  # like swaync: middle-click dismisses
        elif event.button == Gdk.BUTTON_PRIMARY:
            if self._on_close:
                self._on_close()
            self.center.activate(self.notification.id)
        return True

    def refresh_time(self):
        self.time_label.set_label(time_ago(self.notification.time))


class NotificationHub(Box):
    """The bell dropdown: media controls, do-not-disturb and the notification
    history grouped by app. Keys (once clicked into): C clears all, D toggles
    do-not-disturb."""

    def __init__(self, close: Callable[[], None], services: Services, **kwargs):
        self.close = close
        self.center = services.notifications
        self.dnd = services.dnd
        self.media = MediaCard(services.media)

        self.count = Label(name="notif-count")
        # icon-only so the header fits the panel's width
        self.dnd_button = Button(
            name="notif-dnd",
            image=Image(icon_name="notifications-disabled-symbolic", icon_size=14),
            tooltip_text="Do Not Disturb (D)",
            on_clicked=lambda *_: self.dnd.set_enabled(not self.dnd.enabled),
        )
        self.clear_button = Button(name="notif-clear", label="Clear all", on_clicked=lambda *_: self.center.clear())
        self.list = Box(name="notif-list", orientation="v", spacing=12)

        super().__init__(
            name="notif-hub",
            orientation="v",
            spacing=12,
            children=[
                self.media,
                Box(
                    name="notif-header",
                    spacing=8,
                    children=[
                        Label(name="notif-title", label="Notifications"),
                        self.count,
                        Box(h_expand=True),
                        self.dnd_button,
                        self.clear_button,
                    ],
                ),
                ScrolledWindow(
                    name="notif-scroller",
                    child=self.list,
                    h_scrollbar_policy="never",
                    v_scrollbar_policy="automatic",
                    propagate_width=False,
                    propagate_height=True,
                    max_content_size=(-1, LIST_HEIGHT),
                ),
            ],
            **kwargs,
        )
        bind(self, self.center, "changed", lambda *_: self.render())
        bind(self, self.dnd, "changed", lambda *_: self.render_dnd())
        self.render()
        self.render_dnd()

    # -- dropdown hooks ------------------------------------------------------

    def on_open(self):
        self.center.set_hub_open(True)
        self.media.start_ticking()
        for group in self.list.children:
            for child in getattr(group, "children", []):
                if isinstance(child, NotificationCard):
                    child.refresh_time()

    def on_close(self):
        self.center.set_hub_open(False)
        self.media.stop_ticking()

    def on_key(self, event: Gdk.EventKey) -> bool:
        key = (Gdk.keyval_name(event.keyval) or "").lower()
        if key == "c":
            self.center.clear()
        elif key == "d":
            self.dnd.set_enabled(not self.dnd.enabled)
        else:
            return False
        return True

    # -- rendering -----------------------------------------------------------

    def render_dnd(self):
        if self.dnd.enabled:
            self.dnd_button.add_style_class("active")
        else:
            self.dnd_button.remove_style_class("active")

    def render(self):
        for child in self.list.children:
            child.destroy()
        notifications = self.center.notifications()
        self.count.set_label(str(len(notifications)) if notifications else "")
        self.clear_button.set_sensitive(bool(notifications))

        if not notifications:
            self.list.add(
                Box(
                    name="notif-empty",
                    orientation="v",
                    spacing=6,
                    children=[
                        Image(icon_name="preferences-system-notifications-symbolic", icon_size=32),
                        Label(label="No notifications"),
                    ],
                )
            )
            self.list.show_all()
            return

        # group by app, groups ordered by their newest notification
        groups: dict[str, list[Notification]] = {}
        for notification in notifications:
            groups.setdefault(notification.app_name or "Notification", []).append(notification)
        for app, items in groups.items():
            group = Box(name="notif-group", orientation="v", spacing=6)
            if len(items) > 1:
                group.add(
                    Box(
                        name="notif-group-header",
                        spacing=6,
                        children=[
                            Label(name="notif-group-title", label=f"{app} · {len(items)}"),
                            Box(h_expand=True),
                            Button(
                                name="notif-group-clear",
                                label="Clear",
                                on_clicked=lambda *_, a=app: self.center.dismiss_app(a),
                            ),
                        ],
                    )
                )
            for notification in items:
                group.add(NotificationCard(notification, self.center))
            self.list.add(group)
        self.list.show_all()


class NotificationPopups(Window):
    """Toasts at the top right. Mapped only while toasts are showing, and only
    the toasts themselves take clicks."""

    def __init__(self, monitor: Gdk.Monitor, bar, center: NotificationCenter):
        self.center = center
        self.bar = bar
        self.toasts: dict[int, Revealer] = {}
        self._timers: dict[int, int] = {}
        self.stack = Box(name="notif-popups", orientation="v", spacing=8, v_align="start")
        container = Box(children=[self.stack])
        container.set_size_request(POPUP_WIDTH + 24, monitor.get_geometry().height - 100)
        super().__init__(
            name="notif-popup-window",
            title="fabric-notifications",
            layer="overlay",
            anchor="top right",
            exclusivity="none",
            keyboard_mode="none",
            margin=(8, 8, 0, 0),
            visible=False,
            child=container,
        )
        pin_to_monitor(self, monitor)
        self.stack.connect("size-allocate", lambda *_: self.update_input())
        container.show_all()
        bind(self, center, "popup", lambda _, nid: self.show_toast(nid))
        bind(self, center, "popup-closed", lambda _, nid: self.hide_toast(nid, animate=True))
        bind(self, center, "hub-opened", lambda *_: self.hide_all())

    def show_toast(self, nid: int):
        notification = self.center.get(nid)
        if notification is None:
            return
        if nid in self.toasts:  # replaced: swap the content in place
            self.remove_toast(nid)
        card = NotificationCard(notification, self.center, popup=True, on_close=lambda: self.hide_toast(nid))
        card.set_size_request(POPUP_WIDTH, -1)
        card.connect("enter-notify-event", lambda *_: self.pause(nid))
        card.connect("leave-notify-event", lambda _, e: self.resume(nid, notification) if e.detail != Gdk.NotifyType.INFERIOR else None)
        card.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)

        revealer = Revealer(child=card, transition_type="slide-down", transition_duration=220)
        self.toasts[nid] = revealer
        self.stack.add(revealer)
        self.stack.reorder_child(revealer, 0)  # newest on top
        revealer.show_all()
        if not self.get_visible():
            self.show()
        GLib.idle_add(lambda: (revealer.set_reveal_child(True), False)[1])
        self.resume(nid, notification)

        # keep only the newest few on screen (the rest stay in the hub)
        for old in list(self.toasts)[:-MAX_POPUPS]:
            self.hide_toast(old)

    def timeout_for(self, notification: Notification) -> int:
        seconds = TIMEOUT.get(notification.urgency, 6)
        return 0 if seconds == 0 else seconds * 1000

    def pause(self, nid: int):
        if (timer := self._timers.pop(nid, None)) is not None:
            GLib.source_remove(timer)

    def resume(self, nid: int, notification: Notification):
        self.pause(nid)
        if (ms := self.timeout_for(notification)) > 0:
            self._timers[nid] = GLib.timeout_add(ms, lambda: (self.hide_toast(nid), False)[1])

    def hide_toast(self, nid: int, animate: bool = True):
        """Hide the popup only; the notification stays in the hub."""
        self.pause(nid)
        revealer = self.toasts.get(nid)
        if revealer is None:
            return
        if not animate:
            return self.remove_toast(nid)
        revealer.connect("notify::child-revealed", lambda r, _: self.remove_toast(nid) if not r.get_child_revealed() else None)
        revealer.set_reveal_child(False)

    def hide_all(self):
        for nid in list(self.toasts):
            self.hide_toast(nid)

    def remove_toast(self, nid: int):
        self.pause(nid)
        if (revealer := self.toasts.pop(nid, None)) is not None:
            revealer.destroy()
        if not self.toasts:
            self.hide()

    def update_input(self):
        region = cairo.Region()
        for revealer in self.toasts.values():
            alloc = revealer.get_allocation()
            if alloc.width > 1 and alloc.height > 1:
                region.union(cairo.RectangleInt(alloc.x, alloc.y, alloc.width, alloc.height))
        self.input_shape_combine_region(region)


class NotificationButton(Button):
    """Bell with an unread count; the icon shows do-not-disturb."""

    def __init__(self, services: Services, **kwargs):
        self.center = services.notifications
        self.dnd = services.dnd
        self.icon = Image(icon_name="preferences-system-notifications-symbolic", icon_size=16)
        self.badge = Label(name="notif-badge")
        self.badge.set_no_show_all(True)
        super().__init__(
            name="notification-button",
            child=Box(spacing=4, children=[self.icon, self.badge]),
            tooltip_text="Notifications",
            **kwargs,
        )
        bind(self, self.center, "changed", lambda *_: self.refresh())
        bind(self, self.dnd, "changed", lambda *_: self.refresh())
        self.refresh()

    def refresh(self):
        icon = "notifications-disabled-symbolic" if self.dnd.enabled else "preferences-system-notifications-symbolic"
        self.icon.set_from_icon_name(icon, Gtk.IconSize.MENU)
        self.icon.set_pixel_size(16)
        unread = self.center.unread
        self.badge.set_label(str(unread) if unread < 100 else "99+")
        self.badge.set_visible(unread > 0)
