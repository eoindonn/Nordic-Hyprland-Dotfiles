"""Notification daemon (replaces swaync).

Built on Fabric's org.freedesktop.Notifications server, with the parts of the
spec it skips filled in: `replaces_id` updates a notification in place, and an
app closing its own notification is reported back with NotificationClosed.

The center keeps every notification until it's dismissed (a popup timing out
only hides the popup), remembers them across bar restarts, and owns the
do-not-disturb state.
"""

import json
from pathlib import Path
from typing import cast

from fabric.core.service import Service, Signal
from fabric.notifications.service import (
    Notification,
    NotificationCloseReason,
    Notifications,
)
from gi.repository import GLib
from loguru import logger

STATE_DIR = Path(GLib.get_user_cache_dir()) / "fabric-shell"
HISTORY_FILE = STATE_DIR / "notifications.json"
DND_FILE = STATE_DIR / "dnd"
MAX_HISTORY = 100
SAVE_DELAY = 1000  # ms; one write per burst of changes

URGENCY_LOW, URGENCY_NORMAL, URGENCY_CRITICAL = 0, 1, 2


class Daemon(Notifications):
    def do_handle_bus_call(self, conn, sender, path, interface, target, params, invocation, user_data=None):
        if target == "Notify":
            replaces_id = int(params[1])
            if replaces_id and replaces_id in self._notifications:
                # update in place: same id, new content
                notif_id = replaces_id
            else:
                notif_id = self.new_notification_id()
            self._notifications[notif_id] = Notification(
                id=notif_id,
                raw_variant=cast(GLib.Variant, params),
                on_closed=self.do_handle_notification_closed,
                on_action_invoked=self.do_handle_notification_action_invoke,
            )
            self.notification_added(notif_id)
            invocation.return_value(GLib.Variant("(u)", (notif_id,)))
            return conn.flush()
        if target == "CloseNotification":
            notif_id = int(params[0])
            if notif_id in self._notifications:
                # tells the app too, as the spec requires
                self.close_notification(notif_id, NotificationCloseReason.CLOSED_BY_APPLICATION)
            invocation.return_value(None)
            return conn.flush()
        return super().do_handle_bus_call(conn, sender, path, interface, target, params, invocation, user_data)


class DoNotDisturb(Service):
    """While on, popups are suppressed (critical ones still show); everything
    still lands in the notification center."""

    @Signal
    def changed(self) -> None: ...

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.enabled = DND_FILE.exists() and DND_FILE.read_text().strip() == "1"

    def set_enabled(self, enabled: bool):
        if enabled == self.enabled:
            return
        self.enabled = enabled
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            DND_FILE.write_text("1" if enabled else "0")
        except OSError as e:
            logger.warning(f"[Notifications] couldn't save DND state: {e}")
        self.changed()


class NotificationCenter(Service):
    @Signal
    def changed(self) -> None: ...

    @Signal
    def popup(self, notification_id: int) -> None: ...

    @Signal
    def popup_closed(self, notification_id: int) -> None: ...

    @Signal
    def hub_opened(self) -> None: ...

    def __init__(self, dnd: DoNotDisturb, **kwargs):
        super().__init__(**kwargs)
        self.dnd = dnd
        self.unread = 0
        self.hub_open = False  # while open, new notifications don't pop up
        self._known: set[int] = set()  # ids seen, so replacements aren't "new"
        self._save_id: int | None = None
        self.daemon = Daemon()
        self.daemon.connect("notification-added", lambda _, nid: self._on_added(nid))
        self.daemon.connect("notification-removed", lambda _, nid: self._on_removed(nid))
        self._restoring = True
        self._restore()
        self._restoring = False

    # -- reading -------------------------------------------------------------

    def get(self, notification_id: int) -> Notification | None:
        return self.daemon.get_notification_from_id(notification_id)

    def notifications(self) -> list[Notification]:
        """Newest first."""
        return sorted(self.daemon.notifications.values(), key=lambda n: n.time, reverse=True)

    # -- actions -------------------------------------------------------------

    def dismiss(self, notification_id: int):
        if (notification := self.get(notification_id)) is not None:
            notification.close("dismissed-by-user")

    def dismiss_app(self, app_name: str):
        for notification in self.notifications():
            if notification.app_name == app_name:
                notification.close("dismissed-by-user")

    def clear(self):
        for notification in self.notifications():
            notification.close("dismissed-by-user")

    def activate(self, notification_id: int, action: str = "default"):
        """Run an action (the notification's own click action by default), then
        close it unless the app asked for it to stay ("resident")."""
        if (notification := self.get(notification_id)) is None:
            return
        if action == "default" and "default" not in {a.identifier for a in notification.actions}:
            self.dismiss(notification_id)
            return
        notification.invoke_action(action)
        if not notification.do_get_hint_entry("resident"):
            notification.close("dismissed-by-user")

    def set_hub_open(self, open_: bool):
        self.hub_open = open_
        if open_:
            self.hub_opened()  # popups get out of the way, as in swaync
            self.mark_read()

    def mark_read(self):
        if self.unread:
            self.unread = 0
            self.changed()

    # -- daemon events -------------------------------------------------------

    def _on_added(self, notification_id: int):
        notification = self.get(notification_id)
        if notification is None:
            return
        replaced = notification_id in self._known
        self._known.add(notification_id)
        if not self._restoring:
            if not replaced:
                self.unread += 1
            if not self.hub_open and (not self.dnd.enabled or notification.urgency == URGENCY_CRITICAL):
                self.popup(notification_id)
            self._trim()
            self._schedule_save()
        self.changed()

    def _on_removed(self, notification_id: int):
        self._known.discard(notification_id)
        self.unread = min(self.unread, len(self.daemon.notifications))
        self.popup_closed(notification_id)
        self._schedule_save()
        self.changed()

    def _trim(self):
        for notification in self.notifications()[MAX_HISTORY:]:
            self.daemon.remove_notification(notification.id)

    # -- persistence ---------------------------------------------------------

    def _schedule_save(self):
        if self._save_id is not None:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(SAVE_DELAY, self._save)

    def _save(self):
        self._save_id = None
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            data = json.dumps(self.daemon.serialize())
            # write-then-rename, so a crash never leaves half a file
            tmp = HISTORY_FILE.with_suffix(".tmp")
            tmp.write_text(data)
            tmp.replace(HISTORY_FILE)
        except (OSError, TypeError) as e:
            logger.warning(f"[Notifications] couldn't save history: {e}")
        return False

    def _restore(self):
        try:
            data = json.loads(HISTORY_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return
        try:
            self.daemon.deserialize(data[:MAX_HISTORY])
        except Exception as e:
            logger.warning(f"[Notifications] couldn't restore history: {e}")
