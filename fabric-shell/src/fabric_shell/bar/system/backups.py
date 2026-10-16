from dataclasses import dataclass
from datetime import datetime

from fabric.core.service import Service, Signal
from gi.repository import GLib

from ..utils import run_async

POLL_INTERVAL = 30000  # ms
# a home backup older than this is overdue even if nothing has failed (e.g.
# the laptop has been off at midnight for a few days)
OVERDUE_AFTER = 48 * 3600  # s

JOBS = (
    ("restic-home-backup.service", "Home backup"),
    ("restic-etc-backup.service", "/etc backup"),
    ("restic-prune.service", "Repo prune"),
    ("restic-check.service", "Repo check"),
    ("package-backup.service", "Package list"),
    ("timeshift-snapshot-check.service", "Timeshift snapshots"),
)
HOME_BACKUP = JOBS[0][0]
# systemctl only prints the properties that apply to each unit type
PROPERTIES = ("Id", "LoadState", "ActiveState", "Result", "ExecMainExitTimestamp", "LastTriggerUSec")


def timer_for(unit: str) -> str:
    return unit.removesuffix(".service") + ".timer"


@dataclass
class Job:
    unit: str
    label: str
    state: str  # ok, running, failed, never (not run yet), missing (not installed)
    finished: datetime | None  # when it last ran, if known


def job_state(props: dict[str, str], finished: datetime | None) -> str:
    if props.get("LoadState") != "loaded":
        return "missing"
    if props.get("ActiveState") in ("activating", "deactivating"):
        return "running"
    if props.get("ActiveState") == "failed" or props.get("Result", "success") != "success":
        return "failed"
    if finished is None:
        return "never"
    return "ok"


def parse_timestamp(value: str) -> datetime | None:
    # `--timestamp=unix` prints "@<seconds>", or nothing if it never ran
    if not value.startswith("@"):
        return None
    try:
        return datetime.fromtimestamp(int(value[1:]))
    except ValueError:
        return None


class BackupStatus(Service):
    """Health of the backup systemd units, polled from `systemctl show`."""

    @Signal
    def changed(self) -> None: ...

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.jobs: list[Job] = []
        self._busy = False
        self.refresh()
        GLib.timeout_add(POLL_INTERVAL, lambda: (self.refresh(), True)[1])

    @property
    def overdue(self) -> bool:
        home = next((job for job in self.jobs if job.unit == HOME_BACKUP), None)
        if home is None or home.finished is None:
            return True
        return (datetime.now() - home.finished).total_seconds() > OVERDUE_AFTER

    @property
    def overall(self) -> str:
        if not self.jobs:
            return "unknown"
        states = {job.state for job in self.jobs}
        if states & {"failed", "missing"}:
            return "failed"
        if self.overdue:
            return "overdue"
        if "running" in states:
            return "running"
        return "ok"

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        run_async(
            ["systemctl", "show", "--timestamp=unix", f"--property={','.join(PROPERTIES)}"]
            + [name for unit, _ in JOBS for name in (unit, timer_for(unit))],
            self._on_show,
        )

    def _on_show(self, output: bytes):
        self._busy = False
        units = {}
        for block in output.decode(errors="replace").split("\n\n"):
            props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if "Id" in props:
                units[props["Id"]] = props

        jobs = []
        for unit, label in JOBS:
            props = units.get(unit, {})
            # a service's exit time is forgotten on reboot, but persistent
            # timers remember when they last fired
            finished = parse_timestamp(props.get("ExecMainExitTimestamp", "")) or parse_timestamp(
                units.get(timer_for(unit), {}).get("LastTriggerUSec", "")
            )
            jobs.append(Job(unit, label, job_state(props, finished), finished))
        # always notify: "overdue" and the tooltip's relative times move on
        # with the clock even when the units themselves haven't changed
        self.jobs = jobs
        self.changed()
