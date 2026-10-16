from pathlib import Path

from fabric.core.service import Service, Signal
from gi.repository import Gio, GLib
from loguru import logger

from ..utils import run_async

PREVIEW_CHARS = 300
DB_PATH = Path.home() / ".cache" / "cliphist" / "db"
RELOAD_DELAY = 300  # ms; one reload per burst of database writes


class ClipboardStore(Service):
    """cliphist history, refreshed in the background whenever cliphist's
    database changes, so opening the dropdown never has to wait for it."""

    @Signal
    def changed(self) -> None: ...

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lines: list[str] = []
        self._raw: bytes | None = None
        self._reload_id: int | None = None
        self.reload()
        self._watch()

    def _watch(self):
        # cliphist rewrites its database on every store/delete/wipe; watching
        # the file needs no helper process (which would outlive the bar)
        db = Gio.File.new_for_path(str(DB_PATH))
        try:
            self._monitor = db.monitor_file(Gio.FileMonitorFlags.NONE, None)
        except GLib.Error as e:
            logger.warning(f"can't watch cliphist's database: {e.message}")
            return
        self._monitor.connect("changed", lambda *_: self.schedule_reload())

    def schedule_reload(self):
        if self._reload_id is not None:
            GLib.source_remove(self._reload_id)
        self._reload_id = GLib.timeout_add(RELOAD_DELAY, self._on_reload_timeout)

    def _on_reload_timeout(self):
        self._reload_id = None
        self.reload()
        return False

    def reload(self):
        run_async(["cliphist", "list", "-preview-width", str(PREVIEW_CHARS)], self._on_list)

    def _on_list(self, output: bytes):
        if output == self._raw:
            return
        self._raw = output
        self.lines = [line for line in output.decode(errors="replace").splitlines() if line.strip()]
        self.changed()

    def delete(self, line: str):
        run_async(["cliphist", "delete"], stdin=f"{line}\n".encode(), capture=False, on_exit=lambda _: self.reload())

    def wipe(self):
        run_async(["cliphist", "wipe"], capture=False, on_exit=lambda _: self.reload())
