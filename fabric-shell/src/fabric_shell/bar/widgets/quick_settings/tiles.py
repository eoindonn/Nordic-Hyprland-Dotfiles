from collections.abc import Callable

from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.image import Image
from fabric.widgets.label import Label
from gi.repository import Gtk

ICON_SIZE = 16


def set_icon(image: Image, icon_name: str, size: int = ICON_SIZE):
    image.set_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
    image.set_pixel_size(size)


class QuickToggle(Box):
    """GNOME-style pill: the main part toggles, the optional arrow opens a menu."""

    def __init__(
        self,
        icon_name: str,
        title: str,
        on_toggle: Callable[[], None],
        on_expand: Callable[[], None] | None = None,
        **kwargs,
    ):
        self.icon = Image(icon_name=icon_name, icon_size=ICON_SIZE)
        self._icon_name = icon_name
        self.title = Label(
            name="tile-title",
            label=title,
            h_align="start",
            ellipsization="end",
            max_chars_width=16,
        )
        self.subtitle = Label(
            name="tile-subtitle",
            h_align="start",
            ellipsization="end",
            max_chars_width=16,
        )
        # stays hidden while empty, even when the popup calls show_all()
        self.subtitle.set_no_show_all(True)

        children = [
            Button(
                name="tile-main",
                h_expand=True,
                child=Box(
                    spacing=10,
                    children=[
                        self.icon,
                        Box(
                            orientation="v",
                            v_align="center",
                            children=[self.title, self.subtitle],
                        ),
                    ],
                ),
                on_clicked=lambda *_: on_toggle(),
            )
        ]
        if on_expand is not None:
            children.append(
                Button(
                    name="tile-arrow",
                    image=Image(icon_name="go-next-symbolic", icon_size=14),
                    on_clicked=lambda *_: on_expand(),
                )
            )

        super().__init__(name="quick-toggle", h_expand=True, children=children, **kwargs)
        if on_expand is not None:
            self.add_style_class("has-arrow")
        # show the contents now, then let only the panel decide whether the
        # whole tile is visible (it hides tiles for missing hardware)
        self.show_all()
        self.set_no_show_all(True)

    def update(self, active: bool, subtitle: str = "", icon_name: str | None = None):
        # only touch what changed: a label or icon change re-lays-out the
        # whole panel, which stutters an open/close animation
        if active != self.get_style_context().has_class("active"):
            if active:
                self.add_style_class("active")
            else:
                self.remove_style_class("active")
        if subtitle != self.subtitle.get_label():
            self.subtitle.set_label(subtitle)
            self.subtitle.set_visible(bool(subtitle))
        if icon_name is not None and icon_name != self._icon_name:
            self._icon_name = icon_name
            set_icon(self.icon, icon_name)

    def set_expanded(self, expanded: bool):
        if expanded:
            self.add_style_class("expanded")
        else:
            self.remove_style_class("expanded")
