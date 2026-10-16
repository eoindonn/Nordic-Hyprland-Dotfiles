import math

import cairo
from gi.repository import Gdk, GdkPixbuf


def rounded_surface(pixbuf: GdkPixbuf.Pixbuf, radius: float = 8) -> cairo.ImageSurface:
    """The pixbuf with rounded corners (GTK3 CSS can't round an image)."""
    width, height = pixbuf.get_width(), pixbuf.get_height()
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    r = min(radius, width / 2, height / 2)
    cr.new_sub_path()
    cr.arc(width - r, r, r, -math.pi / 2, 0)
    cr.arc(width - r, height - r, r, 0, math.pi / 2)
    cr.arc(r, height - r, r, math.pi / 2, math.pi)
    cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
    cr.clip()
    Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
    cr.paint()
    return surface


def fit(pixbuf: GdkPixbuf.Pixbuf, max_width: int, max_height: int) -> GdkPixbuf.Pixbuf:
    """Scale down (never up) to fit within the box, keeping the aspect ratio."""
    width, height = pixbuf.get_width(), pixbuf.get_height()
    scale = min(max_width / width, max_height / height, 1)
    if scale >= 1:
        return pixbuf
    return pixbuf.scale_simple(max(1, int(width * scale)), max(1, int(height * scale)), GdkPixbuf.InterpType.BILINEAR)
