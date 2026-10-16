import time
from collections.abc import Callable

from fabric.widgets.button import Button
from gi.repository import GLib, Gtk

from ..utils import run_async

CONFIRM_TIMEOUT = 3000  # ms the button stays armed after the first click
# a second click sooner than this is a double-click, not a confirmation
CONFIRM_MIN_GAP = 0.4  # s


class ConfirmButton(Button):
    """Runs its command on the second click; the first only arms it."""

    def __init__(
        self,
        command: list[str],
        action: str,
        on_exit: Callable[[bool], None] | None = None,
        armed_label: str | None = None,
        **kwargs,
    ):
        self._command = command
        self._on_exit = on_exit
        self._armed_label = armed_label
        self._idle_label: str | None = None
        self._armed_at = 0.0
        self._action = action
        self._timeout_id: int | None = None
        super().__init__(
            tooltip_text=action,
            on_clicked=lambda *_: self.on_click(),
            **kwargs,
        )
        self.connect("destroy", lambda *_: self.disarm())

    def on_click(self):
        if self._timeout_id is None:
            self._armed_at = time.monotonic()
            if self._armed_label is not None:
                self._idle_label = self.get_label()
                self.set_label(self._armed_label)
            self.add_style_class("confirm")
            self.set_tooltip_text(f"Click again to {self._action.lower()}")
            self._timeout_id = GLib.timeout_add(CONFIRM_TIMEOUT, self.on_timeout)
            return
        if time.monotonic() - self._armed_at < CONFIRM_MIN_GAP:
            return
        self.disarm()
        run_async(self._command, capture=False, on_exit=self._on_exit)

    def on_timeout(self):
        self._timeout_id = None
        self.reset_style()
        return False

    def disarm(self):
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        self.reset_style()

    def reset_style(self):
        self.remove_style_class("confirm")
        if self._idle_label is not None:
            self.set_label(self._idle_label)
            self._idle_label = None
        self.set_tooltip_text(self._action)


# (css id prefix, nerd font icon, action name, command)
POWER_ACTIONS = [
    ("power-off", "\uf011", "Power off", ["systemctl", "poweroff"]),
    ("reboot", "\uf021", "Reboot", ["systemctl", "reboot"]),
    ("suspend", "\uf04c", "Suspend", ["systemctl", "suspend"]),
]
LOG_OUT = ("log-out", "\uf2f5", "Log out", ["hyprctl", "dispatch", "exit"])


def power_buttons(show_text: bool = False, actions=POWER_ACTIONS) -> list[ConfirmButton]:
    buttons = []
    for key, icon, action, command in actions:
        button = ConfirmButton(
            command,
            action,
            name=f"{key}-button",
            label=f"{icon}  {action}" if show_text else icon,
            armed_label=f"{icon}  Confirm {action.lower()}?" if show_text else None,
        )
        if show_text and isinstance(label := button.get_child(), Gtk.Label):
            label.set_xalign(0)
        buttons.append(button)
    return buttons
