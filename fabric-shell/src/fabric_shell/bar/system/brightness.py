from pathlib import Path

from fabric.core.service import Service, Signal

from ..utils import run_async

MIN_LEVEL = 0.01  # never let the slider turn the screen fully off


class Brightness(Service):
    """Screen backlight; reads sysfs directly, writes through brightnessctl."""

    @Signal
    def changed(self) -> None: ...

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.device = next(iter(sorted(Path("/sys/class/backlight").glob("*"))), None)
        self.level = 0.0
        self._pending: float | None = None
        self._busy = False
        self.refresh()

    @property
    def available(self) -> bool:
        return self.device is not None

    def refresh(self):
        """sysfs doesn't notify on backlight changes, so callers refresh on demand."""
        if self.device is None:
            return
        try:
            current = int((self.device / "brightness").read_text())
            maximum = int((self.device / "max_brightness").read_text())
        except (OSError, ValueError):
            return
        level = current / maximum if maximum else 0.0
        if abs(level - self.level) > 0.005:
            self.level = level
            self.changed()

    def set_level(self, level: float):
        # sliders fire many times a second: keep one brightnessctl in flight
        # and only send the latest value once it finishes
        self._pending = max(MIN_LEVEL, min(level, 1.0))
        if not self._busy:
            self._apply()

    def _apply(self):
        if self._pending is None:
            self._busy = False
            return
        level, self._pending = self._pending, None
        self._busy = True
        self.level = level
        run_async(
            ["brightnessctl", "-q", "set", f"{round(level * 100)}%"],
            capture=False,
            on_exit=lambda _: self._apply(),
        )
