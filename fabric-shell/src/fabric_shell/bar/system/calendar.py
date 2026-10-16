"""Nextcloud calendar over CalDAV.

Events are fetched a month at a time in a worker thread, with recurrences
expanded by the server. Signs in through the desktop client's account (see
nextcloud_account.py).
"""

import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from fabric.core.service import Service, Signal
from gi.repository import GLib
from icalendar import Calendar as ICalendar
from loguru import logger

from .nextcloud_account import NextcloudAccount

REFRESH_INTERVAL = 10 * 60  # s
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "a": "http://apple.com/ns/ical/"}
DEFAULT_COLOR = "#81A1C1"

PROPFIND = b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:a="http://apple.com/ns/ical/">
  <d:prop><d:displayname/><d:resourcetype/><a:calendar-color/><c:supported-calendar-component-set/></d:prop>
</d:propfind>"""

QUERY = """<?xml version="1.0"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data><c:expand start="{start}" end="{end}"/></c:calendar-data></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:time-range start="{start}" end="{end}"/>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""


@dataclass(frozen=True)
class CalendarInfo:
    href: str
    name: str
    color: str


@dataclass(frozen=True)
class Event:
    key: tuple
    summary: str
    location: str
    start: datetime | date  # local time, or a date for all-day events
    end: datetime | date  # exclusive
    calendar: CalendarInfo

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    def start_day(self) -> date:
        return self.start if self.all_day else self.start.date()

    def last_day(self) -> date:
        if self.all_day:
            return self.end - timedelta(days=1)
        # an event ending at midnight doesn't occupy the next day
        end = self.end - timedelta(microseconds=1)
        return max(end.date(), self.start.date())

    def on(self, day: date) -> bool:
        return self.start_day() <= day <= self.last_day()

    def sort_key(self):
        return (not self.all_day, self.start if not self.all_day else datetime.combine(self.start, time()).astimezone())


def month_start(day: date) -> date:
    return day.replace(day=1)


def next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def utc_stamp(day: date) -> str:
    return datetime.combine(day, time()).astimezone().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class CalendarService(Service):
    @Signal
    def changed(self) -> None: ...

    def __init__(self, account: NextcloudAccount, **kwargs):
        super().__init__(**kwargs)
        self.account = account
        self.url, self.user = account.url, account.user
        self.available = account.available
        self.calendars: list[CalendarInfo] = []
        self.status = "Not set up" if not self.available else "Syncing…"
        self.last_sync: datetime | None = None
        self._events: dict[tuple, Event] = {}
        self._months: set[date] = set()  # fetched months
        self._pending: set[date] = set()  # being fetched
        self._lock = threading.Lock()  # one worker at a time
        if self.available:
            today = date.today()
            self.ensure(month_start(today) - timedelta(days=1), next_month(next_month(today)))
            GLib.timeout_add_seconds(REFRESH_INTERVAL, lambda: (self.refresh(), True)[1])

    # -- public --------------------------------------------------------------

    @property
    def web_url(self) -> str:
        return f"{self.url}/apps/calendar" if self.url else ""

    def events_on(self, day: date) -> list[Event]:
        return sorted((e for e in self._events.values() if e.on(day)), key=Event.sort_key)

    def events_between(self, start: date, end: date) -> list[Event]:
        """Events touching [start, end)."""
        return sorted(
            (e for e in self._events.values() if e.start_day() < end and e.last_day() >= start),
            key=Event.sort_key,
        )

    def ensure(self, start: date, end: date):
        """Make sure months covering [start, end) are fetched (in the background)."""
        months, month = [], month_start(start)
        while month < end:
            if month not in self._months and month not in self._pending:
                months.append(month)
            month = next_month(month)
        if months:
            self._pending.update(months)
            threading.Thread(target=self._fetch, args=(months,), daemon=True).start()

    def refresh(self):
        months = sorted(self._months)
        self._months.clear()
        if months:
            self.ensure(months[0], next_month(months[-1]))

    # -- worker thread -------------------------------------------------------

    def _fetch(self, months: list[date]):
        with self._lock:
            try:
                if not self.calendars:
                    self.calendars = self._discover()
                events: dict[tuple, Event] = {}
                for month in months:
                    for calendar in self.calendars:
                        for event in self._query(calendar, month, next_month(month)):
                            events[event.key] = event
                GLib.idle_add(self._merge, months, events, None)
            except Exception as e:  # network, auth or parse failure
                logger.warning(f"[Calendar] sync failed: {e}")
                GLib.idle_add(self._merge, months, {}, str(e))

    def _merge(self, months: list[date], events: dict, error: str | None):
        self._pending.difference_update(months)
        if error is None:
            for month in months:
                # replace what we had for these months
                stale = [k for k, e in self._events.items() if month_start(e.start_day()) == month and k not in events]
                for key in stale:
                    del self._events[key]
            self._events.update(events)
            self._months.update(months)
            self.last_sync = datetime.now()
            self.status = "ok"
        else:
            self.status = "Couldn't reach Nextcloud" if "urlopen" in error or "timed out" in error else error
        self.changed()
        return False

    def _request(self, method: str, url: str, body: bytes, depth: str = "1") -> ET.Element:
        data = self.account.request(
            method,
            url.removeprefix(self.url),
            body,
            {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        return ET.fromstring(data)

    def _discover(self) -> list[CalendarInfo]:
        root = self._request("PROPFIND", f"{self.url}/remote.php/dav/calendars/{self.user}/", PROPFIND)
        calendars = []
        for response in root.findall("d:response", NS):
            prop = response.find("d:propstat/d:prop", NS)
            if prop is None or prop.find("d:resourcetype/c:calendar", NS) is None:
                continue
            components = {c.get("name") for c in prop.findall("c:supported-calendar-component-set/c:comp", NS)}
            if components and "VEVENT" not in components:
                continue  # task-only lists
            color = (prop.findtext("a:calendar-color", "", NS) or DEFAULT_COLOR)[:7]
            calendars.append(
                CalendarInfo(
                    href=response.findtext("d:href", "", NS),
                    name=prop.findtext("d:displayname", "", NS) or "Calendar",
                    color=color,
                )
            )
        return calendars

    def _query(self, calendar: CalendarInfo, start: date, end: date) -> list[Event]:
        body = QUERY.format(start=utc_stamp(start), end=utc_stamp(end)).encode()
        root = self._request("REPORT", f"{self.url}{calendar.href}", body)
        events = []
        for data in root.iterfind(".//c:calendar-data", NS):
            if not data.text:
                continue
            for component in ICalendar.from_ical(data.text).walk("VEVENT"):
                if (event := self._to_event(component, calendar)) is not None:
                    events.append(event)
        return events

    @staticmethod
    def _to_event(component, calendar: CalendarInfo) -> Event | None:
        if (dtstart := component.get("DTSTART")) is None:
            return None
        start = dtstart.dt
        if (dtend := component.get("DTEND")) is not None:
            end = dtend.dt
        elif (duration := component.get("DURATION")) is not None:
            end = start + duration.dt
        else:
            end = start + timedelta(days=1) if not isinstance(start, datetime) else start
        if isinstance(start, datetime):
            # expanded instances arrive in UTC; show them in local time
            start = (start if start.tzinfo else start.replace(tzinfo=timezone.utc)).astimezone()
            end = (end if end.tzinfo else end.replace(tzinfo=timezone.utc)).astimezone()
        uid = str(component.get("UID", ""))
        return Event(
            key=(calendar.href, uid, str(start)),
            summary=str(component.get("SUMMARY", "")) or "(No title)",
            location=str(component.get("LOCATION", "")),
            start=start,
            end=end,
            calendar=calendar,
        )
