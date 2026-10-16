from fabric import Fabricator
from fabric.widgets.circularprogressbar import CircularProgressBar
from fabric.widgets.image import Image

from ..services import bind

BATTERY_CRITICAL = 0.15
BATTERY_STATES = ("charging", "plugged", "discharging", "critical")


class StatRing(CircularProgressBar):
    def __init__(self, source: Fabricator, icon_name: str, icon_size: int, **kwargs):
        super().__init__(
            pie=True,
            child=Image(icon_name=icon_name, icon_size=icon_size),
            size=24,
            **kwargs,
        )
        bind(self, source, "changed", lambda _, value: self.update(value))
        # bars created after startup (hotplugged monitors) show data immediately
        if source.value is not None:
            self.update(source.value)

    def update(self, value):
        self.set_value(value)


class BatteryRing(StatRing):
    def __init__(self, source: Fabricator, **kwargs):
        super().__init__(
            source,
            icon_name="battery-symbolic",
            icon_size=17,
            name="battery-progress-bar",
            **kwargs,
        )

    def update(self, battery):
        if battery is None:
            return

        level = battery.percent / 100
        self.set_value(level)

        if not battery.power_plugged:
            state = "discharging"
        elif battery.percent < 99:
            state = "charging"
        else:
            state = "plugged"

        for style_class in BATTERY_STATES:
            self.remove_style_class(style_class)
        self.add_style_class(state)
        if state == "discharging" and level <= BATTERY_CRITICAL:
            self.add_style_class("critical")

        self.set_tooltip_text(f"Battery {battery.percent:.0f}% ({state})")
