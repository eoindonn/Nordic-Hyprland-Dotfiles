"""Nextcloud Deck boards over Deck's REST API.

Reads and writes happen in a worker thread; the UI only ever sees complete
snapshots, and `changed` fires only when something actually differs, so a
background refresh doesn't rebuild an open dropdown for nothing.
"""

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime

from fabric.core.service import Service, Signal
from gi.repository import GLib
from loguru import logger

from .nextcloud_account import NextcloudAccount

API = "/index.php/apps/deck/api/v1.0"
HEADERS = {"OCS-APIRequest": "true", "Accept": "application/json", "Content-Type": "application/json"}
REFRESH_INTERVAL = 5 * 60  # s
STALE_AFTER = 60  # s; opening the dropdown refreshes if older than this


@dataclass(frozen=True)
class Label:
    title: str
    color: str


@dataclass(frozen=True)
class Card:
    id: int
    board_id: int
    stack_id: int
    title: str
    description: str
    due: datetime | None  # local time
    labels: tuple[Label, ...]
    done: bool
    order: int

    @property
    def overdue(self) -> bool:
        return self.due is not None and not self.done and self.due < datetime.now().astimezone()


@dataclass(frozen=True)
class Stack:
    id: int
    title: str
    cards: tuple[Card, ...]

    @property
    def is_done_column(self) -> bool:
        return self.title.strip().lower() in ("done", "complete", "completed", "finished")


@dataclass(frozen=True)
class Board:
    id: int
    title: str
    color: str
    stacks: tuple[Stack, ...] = field(default_factory=tuple)

    def stack(self, stack_id: int) -> Stack | None:
        return next((s for s in self.stacks if s.id == stack_id), None)


def parse_due(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None


class DeckService(Service):
    @Signal
    def changed(self) -> None: ...

    def __init__(self, account: NextcloudAccount, **kwargs):
        super().__init__(**kwargs)
        self.account = account
        self.available = account.available
        self.boards: tuple[Board, ...] = ()
        self.status = "Syncing…" if self.available else "Nextcloud client isn't set up"
        self.last_sync: datetime | None = None
        self._busy = False
        self._again = False  # another refresh was asked for mid-way
        if self.available:
            self.refresh()
            GLib.timeout_add_seconds(REFRESH_INTERVAL, lambda: (self.refresh(), True)[1])

    # -- reading -------------------------------------------------------------

    def board(self, board_id: int) -> Board | None:
        return next((b for b in self.boards if b.id == board_id), None)

    def card(self, card_id: int) -> Card | None:
        return next((c for b in self.boards for s in b.stacks for c in s.cards if c.id == card_id), None)

    def upcoming(self) -> list[tuple[Board, Stack, Card]]:
        """Cards with a due date that aren't finished, soonest (or most overdue) first."""
        found = [
            (board, stack, card)
            for board in self.boards
            for stack in board.stacks
            if not stack.is_done_column
            for card in stack.cards
            if card.due is not None and not card.done
        ]
        return sorted(found, key=lambda item: item[2].due)

    def web_url(self, board_id: int | None = None, card_id: int | None = None) -> str:
        url = f"{self.account.url}/apps/deck"
        if board_id is not None:
            url += f"/board/{board_id}"
            if card_id is not None:
                url += f"/card/{card_id}"
        return url

    def refresh_if_stale(self):
        if self.last_sync is None or (datetime.now() - self.last_sync).total_seconds() > STALE_AFTER:
            self.refresh()

    def refresh(self):
        if self._busy:
            self._again = True
            return
        self._busy = True
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        try:
            boards = self._load_boards()
            GLib.idle_add(self._apply, boards, None)
        except Exception as e:
            logger.warning(f"[Deck] sync failed: {e}")
            GLib.idle_add(self._apply, None, str(e))

    def _get(self, path: str):
        return json.loads(self.account.request("GET", f"{API}{path}", headers=HEADERS))

    def _load_boards(self) -> tuple[Board, ...]:
        boards = []
        for raw in self._get("/boards"):
            if raw.get("archived") or raw.get("deletedAt"):
                continue
            stacks = []
            for raw_stack in sorted(self._get(f"/boards/{raw['id']}/stacks"), key=lambda s: s.get("order", 0)):
                cards = tuple(
                    Card(
                        id=c["id"],
                        board_id=raw["id"],
                        stack_id=raw_stack["id"],
                        title=c.get("title") or "(Untitled)",
                        description=c.get("description") or "",
                        due=parse_due(c.get("duedate")),
                        labels=tuple(Label(l.get("title", ""), f"#{l.get('color', '888888')}") for l in c.get("labels") or []),
                        done=bool(c.get("done")),
                        order=c.get("order", 0),
                    )
                    for c in sorted(raw_stack.get("cards") or [], key=lambda c: c.get("order", 0))
                    if not c.get("archived") and not c.get("deletedAt")
                )
                stacks.append(Stack(id=raw_stack["id"], title=raw_stack.get("title", ""), cards=cards))
            boards.append(Board(id=raw["id"], title=raw.get("title", ""), color=f"#{raw.get('color', '81a1c1')}", stacks=tuple(stacks)))
        return tuple(boards)

    def _apply(self, boards, error):
        self._busy = False
        if error is None:
            self.last_sync = datetime.now()
            changed = boards != self.boards or self.status != "ok"
            self.boards, self.status = boards, "ok"
        else:
            changed = self.status != error
            self.status = "Couldn't reach Nextcloud" if "urlopen" in error or "timed out" in error else error
        if changed:
            self.changed()
        if self._again:
            self._again = False
            self.refresh()
        return False

    # -- writing -------------------------------------------------------------

    def create_card(self, board_id: int, stack_id: int, title: str, on_done=None):
        body = {"title": title, "type": "plain", "order": 999}
        self._write("POST", f"/boards/{board_id}/stacks/{stack_id}/cards", body, on_done)

    def move_card(self, card: Card, stack_id: int, on_done=None):
        # top of the target column. The URL's stack id must be the *target*:
        # Nextcloud lets URL parameters override the body, so putting the
        # current column there silently turns the move into a no-op.
        body = {"order": 0, "stackId": stack_id}
        self._write("PUT", f"/boards/{card.board_id}/stacks/{stack_id}/cards/{card.id}/reorder", body, on_done)

    def set_done(self, card: Card, done: bool, on_done=None):
        """Mark a card done (and move it to the board's Done column, which is
        what done means on these boards) or not done (left where it is)."""
        board = self.board(card.board_id)
        done_column = next((s for s in board.stacks if s.is_done_column), None) if board else None

        def after_flag(error):
            if error is None and done and done_column is not None and card.stack_id != done_column.id:
                self.move_card(card, done_column.id, on_done)
            elif on_done is not None:
                on_done(error)

        # Deck's REST API has no done endpoint; this is the route its own web
        # UI uses (accepted with the OCS-APIRequest header)
        path = f"/index.php/apps/deck/cards/{card.id}/{'done' if done else 'undone'}"
        self._write("PUT", path, {}, after_flag, prefix="")

    def _write(self, method: str, path: str, body: dict, on_done, prefix: str = API):
        def work():
            error = None
            try:
                self.account.request(method, f"{prefix}{path}", json.dumps(body).encode(), HEADERS)
            except Exception as e:
                logger.warning(f"[Deck] {method} {path} failed: {e}")
                error = str(e)
            GLib.idle_add(finish, error)

        def finish(error):
            if on_done is not None:
                on_done(error)
            self.refresh()
            return False

        threading.Thread(target=work, daemon=True).start()
