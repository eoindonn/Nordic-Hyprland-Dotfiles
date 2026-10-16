import os
from pathlib import Path

from fabric.core.service import Service, Signal
from gi.repository import Gio, GLib

from ..utils import run_async

RECONNECT_DELAY = 5000  # ms between attempts while the client isn't running
REFRESH_INTERVAL = 15000  # ms; re-query in case a push message was missed

# overall states, most important first: the worst folder decides the icon
STATES = ("error", "warning", "syncing", "synced")
DESCRIPTIONS = {
    "synced": "Up to date",
    "syncing": "Syncing…",
    "warning": "Synced with warnings",
    "error": "Sync error",
    "offline": "Offline",  # client running, but no folders are syncing
    "stopped": "Not running",
}


def classify(status: str) -> str:
    # e.g. "OK", "SYNC", "NEW", "ERROR", "WARNING"; "+SWM" marks shared items
    status = status.split("+")[0]
    if status == "ERROR":
        return "error"
    if status == "WARNING":
        return "warning"
    if status in ("SYNC", "NEW"):
        return "syncing"
    return "synced"


class NextcloudSync(Service):
    """Sync status from the Nextcloud desktop client's file-manager socket.

    On connect the client announces each synced folder (REGISTER_PATH); we
    ask for each folder's status and re-ask whenever it says something under
    that folder changed (UPDATE_VIEW / STATUS pushes).
    """

    @Signal
    def changed(self) -> None: ...

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.socket_path = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "Nextcloud" / "socket"
        self.folders: dict[str, str] = {}  # folder path -> state
        self.connected = False
        self._connection: Gio.SocketConnection | None = None
        self._refresh_id: int | None = None
        self._connect()

    # -- state ---------------------------------------------------------------

    @property
    def state(self) -> str:
        if not self.connected:
            return "stopped"
        if not self.folders:
            return "offline"
        return next(s for s in STATES if s in self.folders.values() or s == "synced")

    def open_client(self):
        # the client is single-instance: launching it again shows its window
        run_async(["nextcloud"], capture=False)

    # -- socket --------------------------------------------------------------

    def _connect(self):
        address = Gio.UnixSocketAddress.new(str(self.socket_path))
        Gio.SocketClient().connect_async(address, None, self._on_connected)

    def _on_connected(self, client: Gio.SocketClient, result):
        try:
            self._connection = client.connect_finish(result)
        except GLib.Error:
            return self._schedule_reconnect()

        self.connected = True
        self._read_loop(Gio.DataInputStream(base_stream=self._connection.get_input_stream()))
        self._refresh_id = GLib.timeout_add(REFRESH_INTERVAL, self._refresh_all)
        self.changed()

    def _schedule_reconnect(self):
        GLib.timeout_add(RECONNECT_DELAY, lambda: (self._connect(), False)[1])

    def _on_disconnected(self):
        if self._refresh_id is not None:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = None
        self._connection = None
        self.connected = False
        self.folders.clear()
        self.changed()
        self._schedule_reconnect()

    def _read_loop(self, stream: Gio.DataInputStream):
        def on_line(stream, result):
            try:
                line, _ = stream.read_line_finish_utf8(result)
            except GLib.Error:
                line = None
            if line is None:  # client quit
                return self._on_disconnected()
            self._handle(line)
            self._read_loop(stream)

        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

    def _send(self, message: str):
        if self._connection is None:
            return
        self._connection.get_output_stream().write_bytes_async(
            GLib.Bytes.new(f"{message}\n".encode()), GLib.PRIORITY_DEFAULT, None, None
        )

    # -- protocol ------------------------------------------------------------

    def _handle(self, line: str):
        command, _, args = line.partition(":")
        if command == "REGISTER_PATH":
            self.folders.setdefault(args, "syncing")
            self._send(f"RETRIEVE_FOLDER_STATUS:{args}")
            self.changed()
        elif command == "UNREGISTER_PATH":
            if self.folders.pop(args, None) is not None:
                self.changed()
        elif command == "STATUS":
            status, _, path = args.partition(":")
            if path in self.folders:
                self._set(path, classify(status))
            elif (folder := self._folder_of(path)) is not None:
                # a file inside a folder changed; ask about the whole folder
                self._send(f"RETRIEVE_FOLDER_STATUS:{folder}")
        elif command == "UPDATE_VIEW":
            if (folder := self._folder_of(args)) is not None:
                self._send(f"RETRIEVE_FOLDER_STATUS:{folder}")

    def _folder_of(self, path: str) -> str | None:
        return next(
            (f for f in self.folders if path == f or path.startswith(f.rstrip("/") + "/")),
            None,
        )

    def _set(self, folder: str, state: str):
        if self.folders.get(folder) != state:
            self.folders[folder] = state
            self.changed()

    def _refresh_all(self):
        for folder in self.folders:
            self._send(f"RETRIEVE_FOLDER_STATUS:{folder}")
        return True
