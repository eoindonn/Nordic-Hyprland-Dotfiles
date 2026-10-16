"""Media players over MPRIS, through libplayerctl.

Follows every player that appears, and treats the one playing (or, failing
that, the one most recently active) as current. Cover art is resolved to a
local file: file:// URLs directly, http(s) ones downloaded once in a worker
thread into a small cache.
"""

import hashlib
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Playerctl", "2.0")
from fabric.core.service import Service, Signal
from gi.repository import GLib, Playerctl
from loguru import logger

ART_CACHE = Path(GLib.get_user_cache_dir()) / "fabric-shell" / "art"
ART_TIMEOUT = 10  # s


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    album: str
    length: float  # seconds, 0 if unknown
    art_url: str


def _meta(metadata: GLib.Variant | None, key: str, default=None):
    if metadata is None:
        return default
    value = metadata.lookup_value(key, None)
    return value.unpack() if value is not None else default


class MediaService(Service):
    @Signal
    def changed(self) -> None: ...

    @Signal
    def art_ready(self, path: str) -> None: ...

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.players: list[Playerctl.Player] = []  # most recently active first
        self._art_fetching: set[str] = set()
        try:
            self.manager = Playerctl.PlayerManager()
        except GLib.Error as e:
            logger.warning(f"[Media] playerctl unavailable: {e.message}")
            self.manager = None
            return
        self.manager.connect("name-appeared", lambda _, name: self._add(name))
        self.manager.connect("player-vanished", lambda _, player: self._remove(player))
        for name in self.manager.props.player_names:
            self._add(name)

    # -- players -------------------------------------------------------------

    def _add(self, name: Playerctl.PlayerName):
        try:
            player = Playerctl.Player.new_from_name(name)
        except GLib.Error as e:
            logger.warning(f"[Media] couldn't attach to {name.name}: {e.message}")
            return
        self.manager.manage_player(player)
        for signal in ("metadata", "playback-status", "seeked"):
            player.connect(signal, lambda p, *_: self._on_player_changed(p))
        self.players.append(player)
        self.changed()

    def _remove(self, player: Playerctl.Player):
        if player in self.players:
            self.players.remove(player)
            self.changed()

    def _on_player_changed(self, player: Playerctl.Player):
        # whatever just did something moves to the front
        if self.status(player) == "playing" and player in self.players:
            self.players.remove(player)
            self.players.insert(0, player)
        self.changed()

    @property
    def current(self) -> Playerctl.Player | None:
        playing = next((p for p in self.players if self.status(p) == "playing"), None)
        return playing or (self.players[0] if self.players else None)

    def cycle(self):
        """Make the next player current (when several are open)."""
        if len(self.players) > 1:
            self.players.append(self.players.pop(0))
            self.changed()

    # -- state ---------------------------------------------------------------

    @staticmethod
    def status(player: Playerctl.Player) -> str:
        try:
            return player.props.playback_status.value_nick.lower()  # playing / paused / stopped
        except Exception:
            return "stopped"

    @staticmethod
    def app_name(player: Playerctl.Player) -> str:
        return (player.props.player_name or "Media").replace("_", " ").title()

    def track(self, player: Playerctl.Player) -> Track:
        metadata = player.props.metadata
        artist = _meta(metadata, "xesam:artist", [])
        if isinstance(artist, (list, tuple)):
            artist = ", ".join(a for a in artist if a)
        length = _meta(metadata, "mpris:length", 0) or 0
        return Track(
            title=_meta(metadata, "xesam:title", "") or "Unknown",
            artist=artist or "",
            album=_meta(metadata, "xesam:album", "") or "",
            length=length / 1_000_000,
            art_url=_meta(metadata, "mpris:artUrl", "") or "",
        )

    @staticmethod
    def position(player: Playerctl.Player) -> float:
        """Seconds into the track (0 if the player doesn't report it)."""
        try:
            return player.get_position() / 1_000_000
        except GLib.Error:
            return 0.0

    # -- controls --------------------------------------------------------------

    def play_pause(self):
        self._call("play_pause")

    def next(self):
        self._call("next")

    def previous(self):
        self._call("previous")

    def seek_to(self, seconds: float):
        if (player := self.current) is not None and player.props.can_seek:
            try:
                player.set_position(int(seconds * 1_000_000))
            except GLib.Error as e:
                logger.warning(f"[Media] seek failed: {e.message}")

    def _call(self, method: str):
        if (player := self.current) is None:
            return
        try:
            getattr(player, method)()
        except GLib.Error as e:
            logger.warning(f"[Media] {method} failed: {e.message}")

    # -- artwork -------------------------------------------------------------

    def art_path(self, url: str) -> str | None:
        """Local file for `url` if available now; otherwise starts fetching it
        and emits `art_ready` when done."""
        if not url:
            return None
        if url.startswith("file://"):
            path = GLib.filename_from_uri(url)[0]
            return path if Path(path).exists() else None
        if not url.startswith(("http://", "https://")):
            return None
        target = ART_CACHE / hashlib.sha1(url.encode()).hexdigest()
        if target.exists():
            return str(target)
        if url not in self._art_fetching:
            self._art_fetching.add(url)
            threading.Thread(target=self._download, args=(url, target), daemon=True).start()
        return None

    def _download(self, url: str, target: Path):
        try:
            ART_CACHE.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(url, timeout=ART_TIMEOUT) as response:
                data = response.read()
            tmp = target.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(target)
            GLib.idle_add(lambda: (self.art_ready(str(target)), False)[1])
        except Exception as e:
            logger.warning(f"[Media] couldn't fetch artwork: {e}")
        finally:
            self._art_fetching.discard(url)
