"""One continuous surface for the bar and its dropdowns.

The bar and each dropdown are separate layer surfaces, but they draw a shared
outline: while a dropdown is open the bar leaves a gap in its bottom border
and both surfaces draw the same concave fillets where the dropdown meets the
bar, so the dropdown reads as part of the bar growing downwards.

Colours and border width come from the `#shell-surface` CSS node, so the
stylesheet stays the single source of truth for the theme.
"""

import math
from dataclasses import dataclass

import cairo
from gi.repository import Gdk, Gtk

FILLET = 18  # radius of the concave curve joining a dropdown to the bar
CORNER = 26  # radius of a dropdown's bottom corners

# never shown; they only exist so the CSS for each surface node resolves
_style_widgets: dict[str, Gtk.Widget] = {}


@dataclass
class ShellStyle:
    fill: Gdk.RGBA
    border: Gdk.RGBA
    border_width: float


def shell_style(node: str = "shell-surface") -> ShellStyle:
    """Fill, outline colour and width from the CSS node `#<node>`."""
    if node not in _style_widgets:
        _style_widgets[node] = Gtk.Box(name=node)
    ctx = _style_widgets[node].get_style_context()
    state = Gtk.StateFlags.NORMAL
    return ShellStyle(
        fill=ctx.get_property("background-color", state),
        border=ctx.get_property("border-top-color", state),
        border_width=ctx.get_border(state).top,
    )


@dataclass
class Sheet:
    """A dropdown's outline, in the dropdown's own coordinates.

    `left`/`right` are the x of the body's sides (stroke centre lines),
    `bottom` the y of its bottom edge; the top seam sits at -border/2 so it
    lines up with the bar's bottom border, which is just above y=0.
    """

    left: float
    right: float
    bottom: float
    fillet: float
    corner: float
    border: float

    @property
    def top(self) -> float:
        return -self.border / 2

    @property
    def fillet_y(self) -> float:
        return self.top + self.fillet

    @property
    def gap(self) -> tuple[float, float]:
        """x range where the bar's bottom border must open up."""
        return self.left - self.fillet, self.right + self.fillet


def make_sheet(width: float, height: float, style: ShellStyle) -> Sheet | None:
    b = style.border_width
    left, right = FILLET + b / 2, width - FILLET - b / 2
    bottom = height - b / 2
    available = bottom + b / 2  # from the seam to the bottom edge
    if available < 1 or right <= left:
        return None
    # shrink the curves while the sheet is still short, so it grows smoothly
    fillet = min(FILLET, available / 2)
    corner = max(0.0, min(CORNER - b / 2, available - fillet, (right - left) / 2))
    return Sheet(left, right, bottom, fillet, corner, b)


def sheet_path(cr: cairo.Context, s: Sheet, ox: float = 0, oy: float = 0):
    """Closed outline of a dropdown, offset by (ox, oy)."""
    cr.save()
    cr.translate(ox, oy)
    cr.new_path()
    cr.move_to(s.left - s.fillet, s.top)
    cr.line_to(s.right + s.fillet, s.top)
    cr.arc_negative(s.right + s.fillet, s.fillet_y, s.fillet, 3 * math.pi / 2, math.pi)
    cr.line_to(s.right, s.bottom - s.corner)
    if s.corner:
        cr.arc(s.right - s.corner, s.bottom - s.corner, s.corner, 0, math.pi / 2)
    cr.line_to(s.left + s.corner, s.bottom)
    if s.corner:
        cr.arc(s.left + s.corner, s.bottom - s.corner, s.corner, math.pi / 2, math.pi)
    cr.line_to(s.left, s.fillet_y)
    cr.arc_negative(s.left - s.fillet, s.fillet_y, s.fillet, 0, -math.pi / 2)
    cr.close_path()
    cr.restore()


def fillet_arcs(cr: cairo.Context, s: Sheet, ox: float, oy: float):
    """Just the two concave curves, for the bar to draw its half of them."""
    cr.new_path()
    cr.arc(ox + s.left - s.fillet, oy + s.fillet_y, s.fillet, -math.pi / 2, 0)
    cr.new_sub_path()
    cr.arc(ox + s.right + s.fillet, oy + s.fillet_y, s.fillet, math.pi, 3 * math.pi / 2)


def draw_sheet(cr: cairo.Context, s: Sheet, style: ShellStyle):
    sheet_path(cr, s)
    Gdk.cairo_set_source_rgba(cr, style.fill)
    cr.fill_preserve()
    Gdk.cairo_set_source_rgba(cr, style.border)
    cr.set_line_width(style.border_width)
    cr.stroke()


def draw_bar(
    cr: cairo.Context,
    width: float,
    height: float,
    style: ShellStyle,
    sheets: list[tuple[Sheet, float]],
):
    """Pill-shaped bar; `sheets` are open dropdowns and their x offset in the bar."""
    b = style.border_width
    radius = height / 2

    def pill(inset: float):
        r = radius - inset
        cr.new_path()
        cr.arc(width - radius, radius, r, -math.pi / 2, math.pi / 2)
        cr.arc(radius, radius, r, math.pi / 2, 3 * math.pi / 2)
        cr.close_path()

    pill(0)
    Gdk.cairo_set_source_rgba(cr, style.fill)
    cr.fill()

    pill(b / 2)
    Gdk.cairo_set_source_rgba(cr, style.border)
    cr.set_line_width(b)
    cr.stroke()

    for sheet, ox in sheets:
        x0, x1 = sheet.gap
        # open the bottom border where the dropdown attaches...
        cr.save()
        cr.rectangle(ox + x0, height - b, x1 - x0, b)
        cr.clip()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        Gdk.cairo_set_source_rgba(cr, style.fill)
        cr.paint()
        cr.restore()
        # ...and draw the top half of the curves leading down into it
        fillet_arcs(cr, sheet, ox, height)
        Gdk.cairo_set_source_rgba(cr, style.border)
        cr.set_line_width(b)
        cr.stroke()
