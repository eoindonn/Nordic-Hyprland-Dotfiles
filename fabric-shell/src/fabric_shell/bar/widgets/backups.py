from datetime import datetime, timedelta

from fabric.widgets.image import Image

from ..services import bind
from ..system.backups import BackupStatus, Job

OVERALL_STATES = ("ok", "running", "overdue", "failed", "unknown")
HEADINGS = {
    "ok": "Backups OK",
    "running": "Backup running",
    "overdue": "Backups overdue",
    "failed": "Backup problem",
    "unknown": "Backups: checking…",
}
SYMBOLS = {"ok": "✓", "running": "…", "failed": "✗", "never": "–", "missing": "?"}


def when(moment: datetime) -> str:
    today = datetime.now().date()
    if moment.date() == today:
        return moment.strftime("today %H:%M")
    if moment.date() == today - timedelta(days=1):
        return moment.strftime("yesterday %H:%M")
    return moment.strftime("%-d %b %H:%M")


def describe(job: Job) -> str:
    if job.state == "running":
        return "running now"
    if job.state == "never":
        return "not run yet"
    if job.state == "missing":
        return "unit not installed"
    if job.finished is None:
        return "failed" if job.state == "failed" else "ok"
    return f"{'failed' if job.state == 'failed' else 'last ran'} {when(job.finished)}"


class BackupIndicator(Image):
    def __init__(self, backups: BackupStatus, **kwargs):
        super().__init__(
            name="backup-status",
            icon_name="drive-harddisk-symbolic",
            icon_size=16,
            **kwargs,
        )
        self.backups = backups
        bind(self, backups, "changed", lambda *_: self.update())
        self.update()

    def update(self):
        overall = self.backups.overall
        for style_class in OVERALL_STATES:
            self.remove_style_class(style_class)
        self.add_style_class(overall)

        lines = [HEADINGS[overall]]
        lines += [f"{SYMBOLS[job.state]} {job.label}: {describe(job)}" for job in self.backups.jobs]
        self.set_tooltip_text("\n".join(lines))
