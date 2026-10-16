"""Dropdowns that grow out of the bar.

All of a bar's dropdowns live in one transparent layer surface (the host)
covering the monitor below the bar. It never resizes (resizing a layer
surface makes Hyprland stretch it), so all motion happens inside GTK.

The host is only mapped while a dropdown is open or animating: an unmapped
surface can't take input, so when everything is closed nothing can block
clicks on other apps. While open it takes every click below the bar: clicks
on the dropdown reach its widgets as usual, anything else closes it
(layer-shell has no "clicked outside" event, and keyboard focus can't stand
in for one under focus-follows-mouse).
"""

from collections.abc import Callable

import cairo
from fabric.widgets.box import Box
from fabric.widgets.eventbox import EventBox
from fabric.widgets.wayland import WaylandWindow as Window
from gi.repository import Gdk, GLib, Gtk

from ..utils import pin_to_monitor
from .easing import EMPHASIZED_ACCELERATE, EMPHASIZED_DECELERATE
from .shell import FILLET, Sheet, draw_sheet, make_sheet, sheet_path, shell_style

OPEN_DURATION = 0.42  # s
CLOSE_DURATION = 0.22  # s
SLIDE = 0.25  # how far (as a share of its height) content slides while revealing
WARM_UP_DELAY = 400  # ms after content changes before pre-rendering it
FALLBACK_GRACE = 300  # ms past an animation's length before it's forced to finish


class DropdownHost(Window):
    def __init__(self, monitor: Gdk.Monitor, bar):
        self.bar = bar
        self.dropdowns: list["Dropdown"] = []
        # each sheet gets exactly its own width and the natural height for it
        # (a GtkFixed gives children only their minimum, collapsing
        # scrollable lists to 1px; plain overlay placement lets wide content
        # stretch the sheet)
        self.overlay = Gtk.Overlay(child=Gtk.Box())
        self.overlay.connect("get-child-position", self.position_sheet)
        super().__init__(
            name="dropdown-host",
            title="fabric-dropdown",  # layer namespace, for Hyprland layer rules
            layer="top",
            anchor="top bottom left right",
            exclusivity="none",  # placed below the bar's reserved space
            keyboard_mode="none",
            visible=False,
            child=EventBox(
                events=["button-press"],
                child=self.overlay,
                on_button_press_event=self.on_press,
            ),
        )
        pin_to_monitor(self, monitor)
        self.connect("key-press-event", self.on_key_press)
        self.update_input()
        # ready to show instantly, but not mapped until something opens
        self.get_child().show_all()
        self.realize()

    @property
    def top(self) -> int:
        """Rows of this surface that overlap the bar's bottom edge.

        The surface starts at the bar's reserved zone, which a negative bottom
        margin on the bar pulls up into the bar itself.
        """
        return max(0, -self.bar.margin[2])

    def register(self, dropdown: "Dropdown"):
        self.dropdowns.append(dropdown)
        self.overlay.add_overlay(dropdown.sheet)

    def move(self, dropdown: "Dropdown", x: int):
        dropdown.sheet.queue_resize()

    def position_sheet(self, overlay, widget, allocation):
        dropdown = next((d for d in self.dropdowns if d.sheet is widget), None)
        if dropdown is None:
            return False
        _, height = widget.get_preferred_height_for_width(dropdown.width)
        allocation.x, allocation.y = dropdown.x, self.top
        allocation.width, allocation.height = dropdown.width, height
        return True

    def open_dropdown(self) -> "Dropdown | None":
        return next((d for d in self.dropdowns if d.is_open), None)

    def update_input(self):
        """All clicks below the bar while a dropdown is open, none otherwise
        (including while one is still animating closed); the overlapping rows
        stay with the bar."""
        opening = any(d.is_open for d in self.dropdowns)
        if opening:
            region = cairo.Region(cairo.RectangleInt(0, self.top, 100_000, 100_000))
            # keyboard only once clicked into (e.g. the clipboard search)
            mode = "on-demand"
        else:
            region = cairo.Region()
            mode = "none"  # closed: never steals focus
        self.input_shape_combine_region(region)
        # every sheet's GdkWindow stays mapped (and GtkOverlay keeps them
        # stacked in creation order) even while closed, so a closed sheet
        # would otherwise catch clicks meant for an open one it overlaps
        everywhere = cairo.Region(cairo.RectangleInt(0, 0, 100_000, 100_000))
        for dropdown in self.dropdowns:
            if (window := dropdown.sheet.get_window()) is not None:
                window.input_shape_combine_region(everywhere if dropdown.is_open else cairo.Region(), 0, 0)
        self.keyboard_mode = mode
        if opening and not self.get_visible():
            self.show()

    def release(self):
        """Unmap once nothing is open or still animating."""
        if self.get_visible() and not any(d.is_open or d.progress > 0 for d in self.dropdowns):
            self.hide()

    def on_press(self, _, event: Gdk.EventButton):
        # buttons inside a dropdown handle their own presses; anything that
        # reaches here is either dropdown background or outside it
        for dropdown in self.dropdowns:
            if dropdown.is_open and dropdown.owns(event.window):
                return True
        for dropdown in self.dropdowns:
            dropdown.dismiss()
        return True

    def on_key_press(self, _, event: Gdk.EventKey):
        if (dropdown := self.open_dropdown()) is None:
            return False
        if event.keyval == Gdk.KEY_Escape:
            dropdown.dismiss()
            return True
        if (on_key := getattr(dropdown.content, "on_key", None)) is not None:
            return on_key(event)
        return False


class Dropdown:
    """A sheet that grows out of the bar when its trigger is clicked, and
    closes again on a click anywhere else, on Escape, or when its trigger is
    clicked again.

    `content` is built with a `close` callback and may define `on_open()`,
    `on_close()` and `on_key(event)`.
    """

    def __init__(
        self,
        host: DropdownHost,
        content_factory: Callable[[Callable[[], None]], Gtk.Widget],
        width: int,
        align: str = "center",
        surface: str = "shell-surface",
    ):
        self.host = host
        self.bar = host.bar
        self.align = align  # which edge of the trigger the sheet lines up with
        self.surface = surface  # CSS node for this sheet's fill and outline
        self._trigger: Gtk.Widget | None = None
        self.x = 0  # sheet position on the monitor

        # animation state: progress 0 (closed) .. 1 (open)
        self.progress = 0.0
        self._target = 0.0
        self._from = 0.0
        self._started: float | None = None  # frame time (s) the animation began
        self._tick_id: int | None = None
        self._last_fillet: float | None = None
        self._warm_up_id: int | None = None
        self._fallback_id: int | None = None

        border = int(shell_style().border_width)
        self.width = width + 2 * (FILLET + border)

        self.content = content_factory(self.dismiss)
        self.content.set_hexpand(True)  # fill the sheet's full width
        self.content.set_margin_start(FILLET + border)
        self.content.set_margin_end(FILLET + border)
        self.content.set_margin_bottom(border)

        self.sheet = Box(name="dropdown-sheet", children=[self.content])
        self.sheet.set_size_request(self.width, -1)
        self.sheet.connect("draw", self.on_draw)
        self.sheet.connect("size-allocate", lambda *_: self.on_sheet_resized())
        self.content.connect("size-allocate", lambda *_: self.schedule_warm_up())
        self.sheet.connect("destroy", lambda *_: self._cancel_warm_up())
        host.register(self)

    # -- geometry ------------------------------------------------------------

    def current_sheet(self) -> Sheet | None:
        if self.progress <= 0:
            return None
        alloc = self.sheet.get_allocation()
        return make_sheet(alloc.width, alloc.height * self.progress, self.style())

    def owns(self, window: Gdk.Window | None) -> bool:
        """Whether a click on `window` landed on this sheet.

        The Overlay gives each sheet its own GdkWindow, so a click is inside
        the sheet exactly when it happened in that window or a child of it.
        """
        sheet_window = self.sheet.get_window()
        while window is not None:
            if window == sheet_window:
                return True
            window = window.get_parent()
        return False

    def style(self):
        return shell_style(self.surface)

    def bar_offset(self) -> float:
        """x of the sheet's left edge, in the bar's coordinates."""
        return self.x - self.bar.margin[3]

    def place(self):
        bar_width = self.bar.get_allocated_width()
        monitor_width = bar_width + self.bar.margin[1] + self.bar.margin[3]
        if self.align == "center" or self._trigger is None:
            x = (monitor_width - self.width) // 2
        else:
            coords = self._trigger.translate_coordinates(self.bar, 0, 0)
            trigger_x = coords[-2] if coords else 0
            # line the sheet's body up with the trigger's edge, but keep the
            # fillets clear of the bar's rounded ends
            end_room = self.bar.get_allocated_height() / 2
            if self.align == "left":
                x = self.bar.margin[3] + max(end_room, trigger_x - FILLET)
            else:
                trigger_right = trigger_x + self._trigger.get_allocated_width()
                right = min(bar_width - end_room, trigger_right + FILLET)
                x = self.bar.margin[3] + right - self.width
        self.x = int(x)
        self.host.move(self, self.x)

    def on_sheet_resized(self):
        # content changed size (e.g. a menu inside opened)
        if self._target > 0:
            self.bar.redraw_shape()

    # -- drawing -------------------------------------------------------------

    def schedule_warm_up(self):
        if self.progress > 0:
            return
        self._cancel_warm_up()
        self._warm_up_id = GLib.timeout_add(WARM_UP_DELAY, self.warm_up)

    def _cancel_warm_up(self):
        if self._warm_up_id is not None:
            GLib.source_remove(self._warm_up_id)
            self._warm_up_id = None

    def warm_up(self):
        """Render the content once off-screen while closed.

        A widget's first draw loads icons, shapes text and resolves CSS; done
        on the first animation frame that stalls the opening by ~100ms.
        """
        self._warm_up_id = None
        if self.progress <= 0:
            width, height = self.sheet.get_allocated_width(), self.sheet.get_allocated_height()
            if width > 1 and height > 1:
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
                cr = cairo.Context(surface)
                cr.push_group()
                self.sheet.propagate_draw(self.content, cr)
                cr.pop_group_to_source()
                cr.paint()
        return False

    def on_draw(self, widget, cr: cairo.Context):
        if self.progress <= 0:
            return True  # closed: draw nothing at all

        style = self.style()
        width, full = widget.get_allocated_width(), widget.get_allocated_height()
        visible = full * self.progress
        sheet = make_sheet(width, visible, style)
        if sheet is None:
            return True
        draw_sheet(cr, sheet, style)

        cr.save()
        sheet_path(cr, sheet)
        cr.clip()
        if self.progress >= 1:
            self.sheet.propagate_draw(self.content, cr)
        else:
            # content rides down with the sheet's edge and fades in
            cr.translate(0, (visible - full) * SLIDE)
            cr.push_group()
            self.sheet.propagate_draw(self.content, cr)
            cr.pop_group_to_source()
            cr.paint_with_alpha(min(1.0, max(0.0, (self.progress - 0.15) / 0.6)))
        cr.restore()
        return True

    # -- animation -----------------------------------------------------------

    def animate_to(self, target: float):
        if target == self._target and self._tick_id is not None:
            return
        self._target = target
        self._from = self.progress
        self._started = None
        if self._tick_id is None:
            self._tick_id = self.sheet.add_tick_callback(self.on_tick)
        # frames only come while the compositor sends them; if it stops (it
        # may throttle a surface it considers invisible) don't leave the
        # animation, and with it the dropdown's state, hanging
        if self._fallback_id is not None:
            GLib.source_remove(self._fallback_id)
        duration = OPEN_DURATION if target > self.progress else CLOSE_DURATION
        self._fallback_id = GLib.timeout_add(int(duration * 1000) + FALLBACK_GRACE, self.finish_animation)

    def finish_animation(self):
        self._fallback_id = None
        if self._tick_id is not None:
            self.sheet.remove_tick_callback(self._tick_id)
            self._tick_id = None
            self.progress = self._target
            self.sheet.queue_draw()
            self.bar.redraw_shape()
        if self.progress <= 0:
            self.host.release()
        return False

    def on_tick(self, widget, clock: Gdk.FrameClock):
        now = clock.get_frame_time() / 1e6
        if self._started is None:
            self._started = now
        opening = self._target > self._from
        # a reversal mid-way only covers the remaining distance, so scale time
        duration = (OPEN_DURATION if opening else CLOSE_DURATION) * abs(self._target - self._from)
        t = 1.0 if duration <= 0 else (now - self._started) / duration
        ease = EMPHASIZED_DECELERATE if opening else EMPHASIZED_ACCELERATE
        self.progress = self._from + (self._target - self._from) * ease(t)

        done = t >= 1
        if done:
            self.progress = self._target
            self._tick_id = None
            if self._fallback_id is not None:
                GLib.source_remove(self._fallback_id)
                self._fallback_id = None
            if self.progress <= 0:
                GLib.idle_add(lambda: (self.host.release(), False)[1])

        self.sheet.queue_draw()
        # the bar only needs redrawing while the joining curves still change
        sheet = self.current_sheet()
        fillet = sheet.fillet if sheet else None
        if fillet != self._last_fillet or done:
            self._last_fillet = fillet
            self.bar.redraw_shape()
        return not done

    # -- open / close --------------------------------------------------------

    def attach(self, trigger: Gtk.Widget):
        """Clicking `trigger` toggles the dropdown."""
        self._trigger = trigger
        trigger.connect("clicked", lambda *_: self.toggle())

    @property
    def is_open(self) -> bool:
        return self._target > 0

    def toggle(self):
        self.dismiss() if self.is_open else self.open()

    def open(self):
        if self.is_open:
            return
        if self.progress <= 0:
            self.place()
            if (on_open := getattr(self.content, "on_open", None)) is not None:
                on_open()
        self.bar.dropdown_opened(self)
        self._target = 1.0  # counted as open by the host from here on
        self.host.update_input()  # maps the host
        self._target = 0.0
        self.animate_to(1.0)

    def dismiss(self):
        if not self.is_open:
            return
        if (on_close := getattr(self.content, "on_close", None)) is not None:
            on_close()
        self.animate_to(0.0)
        # stop taking clicks straight away, not when the animation ends
        self.host.update_input()
