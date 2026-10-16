"""Data sources shared by every bar, so adding monitors doesn't add pollers."""

from collections.abc import Callable

import psutil
from fabric import Fabricator
from fabric.core.service import Service
from gi.repository import GObject
from loguru import logger

try:
    from fabric.audio.service import Audio
except Exception as e:  # libcvc missing or broken
    Audio = None
    logger.warning(f"audio service unavailable, falling back to wpctl: {e}")

POLL_INTERVAL = 1000


def bind(
    widget: GObject.Object,
    source: Service,
    signal: str,
    callback: Callable,
) -> None:
    """Connect to a shared source and disconnect again when `widget` dies."""
    handler = source.connect(signal, callback)
    widget.connect("destroy", lambda *_: source.disconnect(handler))


class SystemStats:
    def __init__(self, interval: int = POLL_INTERVAL):
        self.cpu = Fabricator(
            interval=interval,
            poll_from=lambda *_: psutil.cpu_percent() / 100,
        )
        self.ram = Fabricator(
            interval=interval,
            poll_from=lambda *_: psutil.virtual_memory().percent / 100,
        )
        # desktops have no battery; don't poll for one that will never appear
        self.battery = (
            Fabricator(interval=interval, poll_from=lambda *_: psutil.sensors_battery())
            if psutil.sensors_battery() is not None
            else None
        )
        # only used when the Audio service can't be loaded; the command
        # string makes Fabricator spawn it asynchronously
        self._wpctl_volume: Fabricator | None = None
        self._audio = None

    @property
    def audio(self):
        if self._audio is None and Audio is not None:
            self._audio = Audio()
        return self._audio

    @property
    def wpctl_volume(self) -> Fabricator:
        if self._wpctl_volume is None:
            self._wpctl_volume = Fabricator(
                interval=POLL_INTERVAL,
                poll_from="wpctl get-volume @DEFAULT_AUDIO_SINK@",
            )
        return self._wpctl_volume
