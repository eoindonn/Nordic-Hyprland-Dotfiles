from collections.abc import Callable

from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.image import Image
from fabric.widgets.scale import Scale

from .tiles import set_icon

STEP = 0.05


class SliderRow(Box):
    """Icon button plus a slider; programmatic updates don't echo back out."""

    def __init__(
        self,
        icon_name: str,
        on_change: Callable[[float], None],
        on_icon_clicked: Callable[[], None] | None = None,
        on_expand: Callable[[], None] | None = None,
        **kwargs,
    ):
        self._on_change = on_change
        self._syncing = False

        self.icon = Image(icon_name=icon_name, icon_size=18)
        self._icon_name = icon_name
        self.scale = Scale(
            name="quick-slider",
            min_value=0.0,
            max_value=1.0,
            increments=(STEP, STEP * 2),
            h_expand=True,
        )
        self.scale.connect("value-changed", self._on_value_changed)

        children = [
            Button(
                name="slider-icon",
                child=self.icon,
                on_clicked=lambda *_: on_icon_clicked() if on_icon_clicked else None,
            ),
            self.scale,
        ]
        self.arrow = None
        if on_expand is not None:
            self.arrow = Button(
                name="slider-arrow",
                image=Image(icon_name="go-next-symbolic", icon_size=14),
                on_clicked=lambda *_: on_expand(),
            )
            children.append(self.arrow)

        super().__init__(name="slider-row", spacing=8, children=children, **kwargs)
        self.show_all()
        self.set_no_show_all(True)

    def set_expanded(self, expanded: bool):
        if expanded:
            self.add_style_class("expanded")
        else:
            self.remove_style_class("expanded")

    def _on_value_changed(self, scale):
        if not self._syncing:
            self._on_change(scale.get_value())

    def set_value(self, value: float, icon_name: str | None = None):
        value = max(0.0, min(value, 1.0))
        if abs(value - self.scale.get_value()) > 1e-3:
            self._syncing = True
            self.scale.set_value(value)
            self._syncing = False
        if icon_name is not None and icon_name != self._icon_name:
            self._icon_name = icon_name
            set_icon(self.icon, icon_name, 18)


def volume_icon(level: float, muted: bool) -> str:
    if muted or level <= 0:
        return "audio-volume-muted-symbolic"
    if level < 0.34:
        return "audio-volume-low-symbolic"
    if level < 0.67:
        return "audio-volume-medium-symbolic"
    return "audio-volume-high-symbolic"


def mic_icon(level: float, muted: bool) -> str:
    if muted or level <= 0:
        return "microphone-sensitivity-muted-symbolic"
    if level < 0.34:
        return "microphone-sensitivity-low-symbolic"
    if level < 0.67:
        return "microphone-sensitivity-medium-symbolic"
    return "microphone-sensitivity-high-symbolic"
