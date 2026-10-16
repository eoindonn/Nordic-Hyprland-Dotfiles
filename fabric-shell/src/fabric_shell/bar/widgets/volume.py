from fabric.widgets.box import Box
from fabric.widgets.circularprogressbar import CircularProgressBar
from fabric.widgets.eventbox import EventBox
from fabric.widgets.image import Image
from gi.repository import Gdk

from ..services import SystemStats, bind
from ..utils import run_async

STEP = 5  # percent per scroll notch
SINK = "@DEFAULT_AUDIO_SINK@"


class VolumeWidget(Box):
    """Scroll to change volume, click to toggle mute."""

    def __init__(self, stats: SystemStats, **kwargs):
        self.audio = stats.audio
        self._scroll_accum = 0.0

        self.progress_bar = CircularProgressBar(
            name="volume-progress-bar",
            pie=True,
            child=Image(icon_name="audio-speakers-symbolic", icon_size=20),
            size=24,
        )

        super().__init__(
            children=EventBox(
                events=["scroll", "smooth-scroll", "button-press"],
                child=self.progress_bar,
                on_scroll_event=self.on_scroll,
                on_button_press_event=self.on_click,
            ),
            **kwargs,
        )

        if self.audio is not None:
            bind(self, self.audio, "speaker-changed", lambda *_: self.refresh())
            self.refresh()
        else:
            poller = stats.wpctl_volume
            bind(self, poller, "changed", lambda _, out: self.apply_wpctl(out))
            if poller.value is not None:
                self.apply_wpctl(poller.value)

    def refresh(self):
        if (speaker := self.audio.speaker) is None:
            return
        self.show_level(speaker.volume / 100, speaker.muted)

    def apply_wpctl(self, output: str):
        # e.g. "Volume: 0.40" or "Volume: 0.40 [MUTED]"
        parts = output.split()
        if len(parts) < 2:
            return
        self.show_level(float(parts[1]), "[MUTED]" in parts)

    def show_level(self, level: float, muted: bool):
        self.progress_bar.set_value(min(level, 1.0))
        if muted:
            self.progress_bar.add_style_class("muted")
        else:
            self.progress_bar.remove_style_class("muted")
        self.set_tooltip_text(f"Volume {level * 100:.0f}%" + (" (muted)" if muted else ""))

    def on_scroll(self, _, event: Gdk.EventScroll):
        if event.direction == Gdk.ScrollDirection.UP:
            steps = 1
        elif event.direction == Gdk.ScrollDirection.DOWN:
            steps = -1
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            # touchpads send many small deltas; act once per whole notch
            _, _, dy = event.get_scroll_deltas()
            self._scroll_accum -= dy
            steps = int(self._scroll_accum)
            self._scroll_accum -= steps
        else:
            return False

        if steps:
            self.change_volume(steps * STEP)
        return True

    def on_click(self, _, event: Gdk.EventButton):
        if event.button != Gdk.BUTTON_PRIMARY:
            return False
        if self.audio is not None:
            if (speaker := self.audio.speaker) is not None:
                speaker.muted = not speaker.muted
        else:
            run_async(["wpctl", "set-mute", SINK, "toggle"], self.poll_wpctl)
        return True

    def change_volume(self, delta: int):
        if self.audio is not None:
            # the service clamps to its max_volume (100)
            if (speaker := self.audio.speaker) is not None:
                speaker.volume += delta
            return

        sign = "+" if delta > 0 else "-"
        run_async(
            ["wpctl", "set-volume", "-l", "1.0", SINK, f"{abs(delta)}%{sign}"],
            self.poll_wpctl,
        )

    def poll_wpctl(self, *_):
        # runs after the set command exits, so it reads the new value
        run_async(
            ["wpctl", "get-volume", SINK],
            lambda out: self.apply_wpctl(out.decode()),
        )
