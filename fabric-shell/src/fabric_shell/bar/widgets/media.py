from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.image import Image
from fabric.widgets.label import Label
from fabric.widgets.scale import Scale
from gi.repository import GdkPixbuf, GLib, Gtk

from ..services import bind
from ..system.media import MediaService
from .images import rounded_surface

ART_SIZE = 72
POSITION_INTERVAL = 1000  # ms, while visible


def fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class MediaCard(Box):
    """Now playing: artwork, title/artist, seek slider, previous/play/next."""

    def __init__(self, media: MediaService, **kwargs):
        self.media = media
        self._art_url = None
        self._timer: int | None = None
        self._dragging = False

        self.art = Image(name="media-art")
        self.app = Label(name="media-app", h_align="start")
        self.title = Label(name="media-title", h_align="start", ellipsization="end", max_chars_width=30)
        self.artist = Label(name="media-artist", h_align="start", ellipsization="end", max_chars_width=34)
        self.switch = Button(
            name="media-switch",
            image=Image(icon_name="media-playlist-shuffle-symbolic", icon_size=12),
            tooltip_text="Switch player",
            on_clicked=lambda *_: media.cycle(),
        )
        self.switch.set_no_show_all(True)

        self.scale = Scale(name="media-scale", min_value=0, max_value=1, h_expand=True)
        self.scale.connect("change-value", self.on_seek)
        self.elapsed = Label(name="media-time", label="0:00")
        self.total = Label(name="media-time", label="0:00")
        self.timeline = Box(spacing=8, children=[self.elapsed, self.scale, self.total])
        self.timeline.set_no_show_all(True)

        self.prev_button = self.control("media-skip-backward-symbolic", "Previous", media.previous)
        self.play_icon = Image(icon_name="media-playback-start-symbolic", icon_size=18)
        self.play_button = Button(name="media-play", child=self.play_icon, on_clicked=lambda *_: media.play_pause())
        self.next_button = self.control("media-skip-forward-symbolic", "Next", media.next)

        super().__init__(
            name="media",
            orientation="v",
            spacing=8,
            children=[
                Box(
                    spacing=12,
                    children=[
                        self.art,
                        Box(
                            orientation="v",
                            v_align="center",
                            h_expand=True,
                            spacing=2,
                            children=[
                                Box(spacing=6, children=[self.app, Box(h_expand=True), self.switch]),
                                self.title,
                                self.artist,
                            ],
                        ),
                    ],
                ),
                self.timeline,
                Box(
                    spacing=18,
                    h_align="center",
                    children=[self.prev_button, self.play_button, self.next_button],
                ),
            ],
            **kwargs,
        )
        self.show_all()
        self.set_no_show_all(True)
        bind(self, media, "changed", lambda *_: self.refresh())
        bind(self, media, "art-ready", lambda *_: self.refresh(force_art=True))
        self.connect("destroy", lambda *_: self.stop_ticking())
        self.refresh()

    @staticmethod
    def control(icon: str, tooltip: str, action) -> Button:
        return Button(
            name="media-control",
            image=Image(icon_name=icon, icon_size=16),
            tooltip_text=tooltip,
            on_clicked=lambda *_: action(),
        )

    # -- position ticking while the hub is open -----------------------------

    def start_ticking(self):
        if self._timer is None:
            self._timer = GLib.timeout_add(POSITION_INTERVAL, lambda: (self.update_position(), True)[1])
        self.update_position()

    def stop_ticking(self):
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def update_position(self):
        player = self.media.current
        if player is None or self._dragging:
            return
        length = self.media.track(player).length
        position = self.media.position(player)
        if length > 0:
            self.scale.set_value(min(position / length, 1.0))
            self.elapsed.set_label(fmt(position))

    def on_seek(self, scale, scroll, value):
        player = self.media.current
        if player is None:
            return True
        length = self.media.track(player).length
        if length > 0:
            value = max(0.0, min(value, 1.0))
            self.media.seek_to(value * length)
            self.elapsed.set_label(fmt(value * length))
        return False

    # -- state ---------------------------------------------------------------

    def refresh(self, force_art: bool = False):
        player = self.media.current
        self.set_visible(player is not None)
        if player is None:
            return
        track = self.media.track(player)
        playing = self.media.status(player) == "playing"

        self.app.set_label(self.media.app_name(player))
        self.title.set_label(track.title)
        self.artist.set_label(track.artist or track.album)
        self.artist.set_visible(bool(track.artist or track.album))
        self.switch.set_visible(len(self.media.players) > 1)

        self.play_icon.set_from_icon_name(
            "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic", Gtk.IconSize.BUTTON
        )
        self.play_icon.set_pixel_size(18)
        self.play_button.set_tooltip_text("Pause" if playing else "Play")
        self.prev_button.set_sensitive(player.props.can_go_previous)
        self.next_button.set_sensitive(player.props.can_go_next)

        # players without a known length (e.g. some browser media) get no slider
        has_length = track.length > 0
        self.timeline.set_visible(has_length)
        if has_length:
            self.total.set_label(fmt(track.length))
            self.scale.set_sensitive(bool(player.props.can_seek))
            self.update_position()

        if force_art or track.art_url != self._art_url:
            self._art_url = track.art_url
            self.set_art(self.media.art_path(track.art_url))

    def set_art(self, path: str | None):
        if path is None:
            self.art.set_from_icon_name("audio-x-generic-symbolic", Gtk.IconSize.DIALOG)
            self.art.set_pixel_size(ART_SIZE // 2)
            self.art.add_style_class("placeholder")
            return
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, ART_SIZE, ART_SIZE, True)
        except GLib.Error:
            return self.set_art(None)
        self.art.remove_style_class("placeholder")
        self.art.set_from_surface(rounded_surface(pixbuf, radius=12))
