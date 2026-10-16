from collections.abc import Callable

from gi.repository import Gdk, Gio, GLib, GtkLayerShell
from loguru import logger


def run_async(
    argv: list[str],
    callback: Callable[[bytes], None] | None = None,
    stdin: bytes | None = None,
    capture: bool = True,
    on_exit: Callable[[bool], None] | None = None,
    merge_stderr: bool = False,
) -> None:
    """Run a command without blocking the GTK main loop.

    `callback` receives the command's raw stdout once it exits. Pass
    `capture=False` for commands that fork into the background holding
    stdout open (e.g. wl-copy), otherwise we'd wait on them forever.
    `on_exit` receives whether the command exited successfully.
    `merge_stderr` folds error output into what `callback` receives.
    """
    flags = Gio.SubprocessFlags.STDERR_MERGE if merge_stderr else Gio.SubprocessFlags.STDERR_SILENCE
    if capture:
        flags |= Gio.SubprocessFlags.STDOUT_PIPE
    if stdin is not None:
        flags |= Gio.SubprocessFlags.STDIN_PIPE

    try:
        process = Gio.Subprocess.new(argv, flags)
    except GLib.Error as e:
        logger.error(f"failed to run {argv[0]}: {e.message}")
        if on_exit is not None:
            on_exit(False)
        return

    def on_done(proc: Gio.Subprocess, result: Gio.AsyncResult):
        try:
            _, stdout, _ = proc.communicate_finish(result)
        except GLib.Error as e:
            logger.error(f"{argv[0]} failed: {e.message}")
            if on_exit is not None:
                on_exit(False)
            return
        if callback is not None:
            callback(stdout.get_data() if stdout is not None else b"")
        if on_exit is not None:
            on_exit(proc.get_successful())

    process.communicate_async(
        GLib.Bytes.new(stdin) if stdin is not None else None, None, on_done
    )


def pin_to_monitor(window, monitor: Gdk.Monitor) -> None:
    """Place a layer-shell window on `monitor`.

    Fabric's `monitor=` argument silently ignores Gdk.Monitor objects (it only
    handles indices, which shift as monitors come and go), so set it directly.
    """
    GtkLayerShell.set_monitor(window, monitor)

