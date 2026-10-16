import calendar as cal
from collections.abc import Callable
from datetime import date, datetime, timedelta

from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.image import Image
from fabric.widgets.label import Label
from fabric.widgets.scrolledwindow import ScrolledWindow
from gi.repository import Gdk, GLib, Gtk

from ..services import bind
from ..system.calendar import CalendarService, Event, month_start, next_month
from ..utils import run_async
from .colors import visible_accent

VIEWS = ("month", "week", "day", "upcoming")
VIEW_LABELS = {"month": "Month", "week": "Week", "day": "Day", "upcoming": "Upcoming"}
UPCOMING_DAYS = 14
MAX_DOTS = 3
LIST_HEIGHT = 300


def fmt_time(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def event_when(event: Event, day: date | None = None) -> str:
    if event.all_day:
        days = (event.end - event.start).days
        return "All day" if days <= 1 else f"All day · {days} days"
    if day is not None and event.start.date() < day:
        return f"Until {fmt_time(event.end)}" if event.end.date() == day else "All day"
    return f"{fmt_time(event.start)} – {fmt_time(event.end)}"


def day_title(day: date) -> str:
    today = date.today()
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return day.strftime("%A %-d %B")


class CalendarPanel(Box):
    """Month / week / day / upcoming views of the Nextcloud calendar.

    Switch views with the tabs (or 1–4 once clicked into), move with ‹ ›
    (or ← →), and jump back with Today (or T).
    """

    def __init__(self, close: Callable[[], None], service: CalendarService, **kwargs):
        self.close = close
        self.service = service
        self.view = "month"
        self.selected = date.today()  # the day the views centre on

        self.title = Label(name="cal-title", h_align="start", h_expand=True, ellipsization="end")
        header = Box(
            name="cal-header",
            spacing=4,
            children=[
                self.title,
                self.nav_button("go-previous-symbolic", "Previous", lambda: self.step(-1)),
                Button(name="cal-today", label="Today", on_clicked=lambda *_: self.go_today()),
                self.nav_button("go-next-symbolic", "Next", lambda: self.step(1)),
            ],
        )

        self.tabs: dict[str, Button] = {}
        for view in VIEWS:
            self.tabs[view] = Button(
                name="cal-tab",
                label=VIEW_LABELS[view],
                h_expand=True,
                on_clicked=lambda *_, v=view: self.set_view(v),
            )
        tabs = Box(name="cal-tabs", spacing=2, children=list(self.tabs.values()))
        tabs.set_homogeneous(True)

        # holds the current view's page; a plain box always takes the page's
        # real size (a GtkStack's size interpolation stalled here, collapsing
        # the view to nothing)
        self.body = Box(name="cal-body", orientation="v")

        self.status = Label(name="cal-status", h_align="start", h_expand=True)
        footer = Box(
            name="cal-footer",
            spacing=6,
            children=[
                self.status,
                Button(
                    name="cal-open",
                    label="Open Calendar",
                    on_clicked=lambda *_: self.open_web(),
                ),
            ],
        )

        super().__init__(
            name="calendar",
            orientation="v",
            spacing=10,
            children=[header, tabs, self.body, footer],
            **kwargs,
        )
        bind(self, service, "changed", lambda *_: self.render())
        self.render()

    @staticmethod
    def nav_button(icon: str, tooltip: str, action) -> Button:
        return Button(
            name="cal-nav",
            image=Image(icon_name=icon, icon_size=14),
            tooltip_text=tooltip,
            on_clicked=lambda *_: action(),
        )

    # -- dropdown hooks ------------------------------------------------------

    def on_open(self):
        # always open on today, in the view last used
        if self.selected != date.today():
            self.selected = date.today()
            self.render()

    def on_key(self, event: Gdk.EventKey) -> bool:
        key = Gdk.keyval_name(event.keyval) or ""
        if key in ("1", "2", "3", "4"):
            self.set_view(VIEWS[int(key) - 1])
        elif key == "Left":
            self.step(-1)
        elif key == "Right":
            self.step(1)
        elif key.lower() == "t":
            self.go_today()
        elif key == "Tab":
            self.set_view(VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)])
        else:
            return False
        return True

    # -- navigation ----------------------------------------------------------

    def set_view(self, view: str):
        if view != self.view:
            self.view = view
            self.render()

    def go_today(self):
        self.selected = date.today()
        self.render()

    def step(self, direction: int):
        if self.view == "month":
            anchor = month_start(self.selected)
            target = next_month(anchor) if direction > 0 else month_start(anchor - timedelta(days=1))
            # keep the same day of the month where possible
            last = cal.monthrange(target.year, target.month)[1]
            self.selected = target.replace(day=min(self.selected.day, last))
        elif self.view == "week":
            self.selected += timedelta(weeks=direction)
        elif self.view == "day":
            self.selected += timedelta(days=direction)
        else:
            self.selected += timedelta(days=UPCOMING_DAYS * direction)
        self.render()

    def select_day(self, day: date):
        self.selected = day
        self.render()

    def open_web(self, day: date | None = None):
        if not self.service.web_url:
            return
        url = self.service.web_url
        if day is not None:
            url += f"/timeGridDay/{day.isoformat()}"
        run_async(["xdg-open", url], capture=False)
        self.close()

    # -- rendering -----------------------------------------------------------

    def render(self):
        for view, tab in self.tabs.items():
            if view == self.view:
                tab.add_style_class("active")
            else:
                tab.remove_style_class("active")

        start, end = self.visible_range()
        self.service.ensure(start - timedelta(days=7), end + timedelta(days=7))

        for child in self.body.children:
            child.destroy()
        page = Box(name="cal-page", orientation="v", spacing=8)
        getattr(self, f"render_{self.view}")(page)
        self.body.add(page)
        page.show_all()
        # fade the new view in: the class change on the next frame is what
        # starts the CSS opacity transition
        page.add_style_class("entering")
        GLib.idle_add(lambda: (page.remove_style_class("entering"), False)[1])
        self.render_status()

    def visible_range(self) -> tuple[date, date]:
        if self.view == "month":
            first = month_start(self.selected)
            return first, next_month(first)
        if self.view == "week":
            monday = self.selected - timedelta(days=self.selected.weekday())
            return monday, monday + timedelta(days=7)
        if self.view == "day":
            return self.selected, self.selected + timedelta(days=1)
        return self.selected, self.selected + timedelta(days=UPCOMING_DAYS)

    def render_status(self):
        service = self.service
        if not service.available:
            text = "Nextcloud client isn't set up"
        elif service.status == "ok" and service.last_sync is not None:
            minutes = int((datetime.now() - service.last_sync).total_seconds() // 60)
            text = "Synced just now" if minutes < 1 else f"Synced {minutes} min ago"
        else:
            text = service.status
        self.status.set_label(text)

    # month -------------------------------------------------------------------

    def render_month(self, page: Box):
        first = month_start(self.selected)
        self.title.set_label(first.strftime("%B %Y"))

        grid = Gtk.Grid(name="cal-grid", column_homogeneous=True, row_spacing=2, column_spacing=2)
        for column, name in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):
            grid.attach(Label(name="cal-weekday", label=name), column, 0, 1, 1)

        day = first - timedelta(days=first.weekday())
        today = date.today()
        weeks = -(-(first.weekday() + cal.monthrange(first.year, first.month)[1]) // 7)
        for row in range(1, weeks + 1):
            for column in range(7):
                grid.attach(self.day_cell(day, first, today), column, row, 1, 1)
                day += timedelta(days=1)
        page.add(grid)

        page.add(Label(name="cal-day-heading", label=day_title(self.selected), h_align="start"))
        page.add(self.event_list(self.service.events_on(self.selected), self.selected, empty="Nothing scheduled"))

    def day_cell(self, day: date, month: date, today: date) -> Button:
        events = self.service.events_on(day)
        dots = Box(name="cal-dots", spacing=2, h_align="center")
        for color in list(dict.fromkeys(e.calendar.color for e in events))[:MAX_DOTS]:
            dots.add(Box(name="cal-dot", style=f"background-color: {visible_accent(color)};"))
        cell = Button(
            name="cal-day",
            child=Box(
                orientation="v",
                spacing=2,
                children=[Label(label=str(day.day)), dots],
            ),
            on_clicked=lambda *_: self.select_day(day),
        )
        cell.set_tooltip_text(f"{len(events)} event{'s' if len(events) != 1 else ''}" if events else None)
        if day.month != month.month:
            cell.add_style_class("other-month")
        if day == today:
            cell.add_style_class("today")
        if day == self.selected:
            cell.add_style_class("selected")
        return cell

    # week ------------------------------------------------------------------

    def render_week(self, page: Box):
        monday = self.selected - timedelta(days=self.selected.weekday())
        sunday = monday + timedelta(days=6)
        week = monday.isocalendar().week
        span = f"{monday:%-d %b}–{sunday:%-d %b}" if monday.month != sunday.month else f"{monday:%-d}–{sunday:%-d %b}"
        self.title.set_label(f"Week {week} · {span}")

        days = Box(orientation="v", spacing=10)
        for offset in range(7):
            day = monday + timedelta(days=offset)
            heading = Label(name="cal-day-heading", label=day.strftime("%A %-d"), h_align="start")
            if day == date.today():
                heading.add_style_class("today")
            days.add(heading)
            events = self.service.events_on(day)
            if events:
                days.add(self.event_rows(events, day))
            else:
                days.add(Label(name="cal-empty-inline", label="No events", h_align="start"))
        page.add(self.scrolled(days))

    # day -------------------------------------------------------------------

    def render_day(self, page: Box):
        self.title.set_label(self.selected.strftime("%A %-d %B"))
        page.add(self.event_list(self.service.events_on(self.selected), self.selected, empty="Nothing scheduled"))

    # upcoming --------------------------------------------------------------

    def render_upcoming(self, page: Box):
        start = self.selected
        self.title.set_label("Upcoming" if start == date.today() else f"From {start:%-d %B}")
        days = Box(orientation="v", spacing=10)
        found = False
        for offset in range(UPCOMING_DAYS):
            day = start + timedelta(days=offset)
            events = [e for e in self.service.events_on(day) if not (not e.all_day and e.end < datetime.now().astimezone())]
            if not events:
                continue
            found = True
            heading = Label(name="cal-day-heading", label=day_title(day), h_align="start")
            if day == date.today():
                heading.add_style_class("today")
            days.add(heading)
            days.add(self.event_rows(events, day))
        if not found:
            days.add(self.empty(f"Nothing in the next {UPCOMING_DAYS} days"))
        page.add(self.scrolled(days))

    # shared ----------------------------------------------------------------

    def event_list(self, events: list[Event], day: date, empty: str) -> Gtk.Widget:
        if not events:
            return self.empty(empty)
        return self.scrolled(self.event_rows(events, day))

    def event_rows(self, events: list[Event], day: date) -> Box:
        rows = Box(orientation="v", spacing=4)
        now = datetime.now().astimezone()
        for event in events:
            bar = Box(name="cal-event-bar", style=f"background-color: {visible_accent(event.calendar.color)};")
            details = [event_when(event, day)]
            if event.location:
                details.append(event.location)
            text = Box(
                orientation="v",
                v_align="center",
                h_expand=True,
                children=[
                    Label(name="cal-event-title", label=event.summary, h_align="start", ellipsization="end", max_chars_width=34),
                    Label(name="cal-event-detail", label=" · ".join(details), h_align="start", ellipsization="end", max_chars_width=40),
                ],
            )
            row = Button(
                name="cal-event",
                child=Box(spacing=10, children=[bar, text]),
                tooltip_text=f"{event.calendar.name}\nClick to open in Nextcloud",
                on_clicked=lambda *_, d=day: self.open_web(d),
            )
            if not event.all_day and event.end < now:
                row.add_style_class("past")
            elif not event.all_day and event.start <= now < event.end:
                row.add_style_class("now")
            rows.add(row)
        return rows

    @staticmethod
    def scrolled(child: Gtk.Widget) -> ScrolledWindow:
        return ScrolledWindow(
            name="cal-scroller",
            child=child,
            h_scrollbar_policy="never",
            v_scrollbar_policy="automatic",
            propagate_width=True,
            propagate_height=True,
            max_content_size=(-1, LIST_HEIGHT),
        )

    @staticmethod
    def empty(text: str) -> Box:
        return Box(
            name="cal-empty",
            orientation="v",
            spacing=6,
            children=[
                Image(icon_name="x-office-calendar-symbolic", icon_size=28),
                Label(label=text),
            ],
        )
