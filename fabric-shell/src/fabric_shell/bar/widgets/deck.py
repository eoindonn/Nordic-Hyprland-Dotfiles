from collections.abc import Callable
from datetime import datetime, timedelta

from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.entry import Entry
from fabric.widgets.image import Image
from fabric.widgets.label import Label
from fabric.widgets.scrolledwindow import ScrolledWindow
from gi.repository import GLib, Gtk

from ..services import bind
from ..system.deck import Board, Card, DeckService, Stack
from ..utils import run_async
from . import markdown
from .colors import text_on, visible_accent

LIST_HEIGHT = 340
UPCOMING = "upcoming"


def due_text(due: datetime) -> str:
    now = datetime.now().astimezone()
    days = (due.date() - now.date()).days
    if due < now:
        return f"Overdue · {due:%-d %b}"
    if days == 0:
        return f"Today · {due:%H:%M}"
    if days == 1:
        return "Tomorrow"
    if days < 7:
        return due.strftime("%A")
    return due.strftime("%-d %b")


def upcoming_group(card: Card) -> str:
    now = datetime.now().astimezone()
    days = (card.due.date() - now.date()).days
    if card.overdue:
        return "Overdue"
    if days == 0:
        return "Today"
    if days < 7:
        return "This week"
    return "Later"


class DeckPanel(Box):
    """Nextcloud Deck: board / upcoming views, card details, add and move cards."""

    def __init__(self, close: Callable[[], None], service: DeckService, **kwargs):
        self.close = close
        self.service = service
        self.view = UPCOMING  # or a board id
        self.stack_ids: dict[int, int] = {}  # selected column per board
        self.open_card: int | None = None  # card shown in detail
        self.adding = False

        self.chips = Box(name="deck-chips", spacing=4)
        self.body = Box(name="deck-body", orientation="v")
        self.status = Label(name="deck-status", h_align="start", h_expand=True)
        self.open_deck_button = Button(name="deck-open", label="Open Deck", on_clicked=lambda *_: self.open_web())
        # card detail has its own "Open in Deck"; don't show two
        self.open_deck_button.set_no_show_all(True)

        super().__init__(
            name="deck",
            orientation="v",
            spacing=10,
            children=[
                Label(name="deck-title", label="Deck", h_align="start"),
                # boards scroll sideways (mouse wheel works) rather than
                # widening the dropdown when they don't all fit
                ScrolledWindow(
                    name="deck-chips-scroller",
                    child=self.chips,
                    h_scrollbar_policy="external",
                    v_scrollbar_policy="never",
                    propagate_width=False,
                    propagate_height=True,
                ),
                self.body,
                Box(
                    name="deck-footer",
                    spacing=6,
                    children=[
                        self.status,
                        self.open_deck_button,
                    ],
                ),
            ],
            **kwargs,
        )
        bind(self, service, "changed", lambda *_: self.render())
        self.render()

    # -- dropdown hooks ------------------------------------------------------

    def on_open(self):
        self.service.refresh_if_stale()

    def on_close(self):
        # come back to the list rather than a card or a half-typed title
        if self.open_card is not None or self.adding:
            self.open_card, self.adding = None, False
            self.render()

    # -- navigation ----------------------------------------------------------

    def show_view(self, view):
        self.view, self.open_card, self.adding = view, None, False
        self.render()

    def show_stack(self, board: Board, stack: Stack):
        self.stack_ids[board.id] = stack.id
        self.adding = False
        self.render()

    def show_card(self, card: Card):
        self.open_card = card.id
        self.render()

    def back(self):
        self.open_card = None
        self.render()

    def open_web(self, board_id: int | None = None, card_id: int | None = None):
        if self.service.available:
            run_async(["xdg-open", self.service.web_url(board_id, card_id)], capture=False)
            self.close()

    # -- rendering -----------------------------------------------------------

    def render(self):
        self.render_chips()
        for child in self.body.children:
            child.destroy()
        page = Box(name="deck-page", orientation="v", spacing=8)

        card = self.service.card(self.open_card) if self.open_card is not None else None
        board = self.service.board(self.view) if self.view != UPCOMING else None
        if self.view != UPCOMING and board is None and self.service.boards:
            self.view = UPCOMING  # board was deleted or archived

        if card is not None:
            self.render_card(page, card)
        elif self.view == UPCOMING:
            self.render_upcoming(page)
        elif board is not None:
            self.render_board(page, board)
        else:
            page.add(self.empty("Loading boards…" if self.service.status != "ok" else "No boards"))

        self.body.add(page)
        page.show_all()
        self.open_deck_button.set_visible(card is None)
        # fade the new page in; the class change is what starts the transition
        page.add_style_class("entering")
        GLib.idle_add(lambda: (page.remove_style_class("entering"), False)[1])
        self.render_status()

    def render_status(self):
        service = self.service
        if service.status == "ok" and service.last_sync is not None:
            minutes = int((datetime.now() - service.last_sync).total_seconds() // 60)
            self.status.set_label("Synced just now" if minutes < 1 else f"Synced {minutes} min ago")
        else:
            self.status.set_label(service.status)

    def render_chips(self):
        for child in self.chips.children:
            child.destroy()
        upcoming = len(self.service.upcoming())
        items = [(UPCOMING, "Upcoming" + (f" {upcoming}" if upcoming else ""), None)]
        items += [(b.id, b.title, b.color) for b in self.service.boards]
        for view, title, color in items:
            children = []
            if color:
                children.append(Box(name="deck-chip-dot", v_align="center", style=f"background-color: {visible_accent(color)};"))
            children.append(Label(label=title))
            chip = Button(
                name="deck-chip",
                child=Box(spacing=6, children=children),
                on_clicked=lambda *_, v=view: self.show_view(v),
            )
            if view == self.view:
                chip.add_style_class("active")
            self.chips.add(chip)
        self.chips.show_all()

    # board -----------------------------------------------------------------

    def render_board(self, page: Box, board: Board):
        if not board.stacks:
            page.add(self.empty("This board has no columns"))
            return
        stack = board.stack(self.stack_ids.get(board.id, -1)) or board.stacks[0]
        self.stack_ids[board.id] = stack.id

        tabs = Box(name="deck-tabs", spacing=2)
        tabs.set_homogeneous(True)
        for column in board.stacks:
            tab = Button(
                name="deck-tab",
                child=Box(
                    spacing=4,
                    h_align="center",
                    children=[
                        Label(label=column.title, ellipsization="end", max_chars_width=10),
                        Label(name="deck-tab-count", label=str(len(column.cards))),
                    ],
                ),
                tooltip_text=column.title,
                on_clicked=lambda *_, c=column: self.show_stack(board, c),
            )
            if column.id == stack.id:
                tab.add_style_class("active")
            tabs.add(tab)
        page.add(tabs)

        cards = Box(orientation="v", spacing=6)
        for card in stack.cards:
            cards.add(self.card_row(card))
        if not stack.cards:
            cards.add(Label(name="deck-empty-inline", label="No cards", h_align="start"))
        page.add(self.scrolled(cards))
        page.add(self.add_card_widget(board, stack))

    def add_card_widget(self, board: Board, stack: Stack) -> Gtk.Widget:
        if not self.adding:
            return Button(
                name="deck-add",
                child=Box(
                    spacing=8,
                    children=[Image(icon_name="list-add-symbolic", icon_size=14), Label(label=f"Add card to {stack.title}")],
                ),
                on_clicked=lambda *_: self.start_adding(),
            )

        entry = Entry(name="deck-add-entry", placeholder="Card title", h_expand=True)

        def submit(*_):
            title = entry.get_text().strip()
            if not title:
                return
            entry.set_sensitive(False)
            self.adding = False
            self.service.create_card(board.id, stack.id, title, on_done=self.on_write_done)

        entry.connect("activate", submit)
        GLib.idle_add(lambda: (entry.grab_focus(), False)[1])
        return Box(
            spacing=6,
            children=[
                entry,
                Button(name="deck-add-confirm", label="Add", on_clicked=submit),
                Button(
                    name="deck-add-cancel",
                    image=Image(icon_name="window-close-symbolic", icon_size=12),
                    on_clicked=lambda *_: self.cancel_adding(),
                ),
            ],
        )

    def start_adding(self):
        self.adding = True
        self.render()

    def cancel_adding(self):
        self.adding = False
        self.render()

    def on_write_done(self, error: str | None):
        # always replace the "Moving…" / "Marking…" message, even if the
        # refresh that follows finds nothing new to redraw
        self.status.set_label("Couldn't save to Deck" if error is not None else "Saved")

    # upcoming --------------------------------------------------------------

    def render_upcoming(self, page: Box):
        items = self.service.upcoming()
        if not items:
            page.add(self.empty("No cards due"))
            return
        rows = Box(orientation="v", spacing=6)
        group = None
        for board, stack, card in items:
            if (current := upcoming_group(card)) != group:
                group = current
                heading = Label(name="deck-group", label=group, h_align="start")
                if group == "Overdue":
                    heading.add_style_class("overdue")
                rows.add(heading)
            rows.add(self.card_row(card, context=f"{board.title} · {stack.title}", color=board.color))
        page.add(self.scrolled(rows))

    # card detail -----------------------------------------------------------

    def render_card(self, page: Box, card: Card):
        board = self.service.board(card.board_id)
        stack = board.stack(card.stack_id) if board else None

        page.add(
            Button(
                name="deck-back",
                child=Box(
                    spacing=6,
                    children=[
                        Image(icon_name="go-previous-symbolic", icon_size=14),
                        Label(label=board.title if board else "Back"),
                    ],
                ),
                h_align="start",
                on_clicked=lambda *_: self.back(),
            )
        )
        title = Label(name="deck-card-title", label=card.title, h_align="start", line_wrap="word-char")
        title.set_xalign(0)
        title.set_max_width_chars(40)
        page.add(title)

        meta = Box(spacing=6)
        if stack is not None:
            meta.add(Label(name="deck-pill", label=stack.title))
        if card.due is not None:
            due = Label(name="deck-pill", label=due_text(card.due))
            due.add_style_class("due")
            if card.overdue:
                due.add_style_class("overdue")
            meta.add(due)
        for label in card.labels:
            meta.add(self.label_pill(label))
        page.add(meta)

        if card.description.strip():
            description = markdown.render(card.description.strip(), on_link=self.open_link)
            page.add(self.scrolled(Box(name="deck-description", children=[description]), height=240))
        else:
            page.add(Label(name="deck-empty-inline", label="No description", h_align="start"))

        if board is not None and len(board.stacks) > 1:
            page.add(Label(name="deck-group", label="Move to", h_align="start"))
            moves = Box(name="deck-moves", spacing=4)
            moves.set_homogeneous(True)
            for column in board.stacks:
                button = Button(
                    name="deck-move",
                    child=Label(label=column.title, ellipsization="end", max_chars_width=10),
                    tooltip_text=column.title,
                    on_clicked=lambda *_, c=column: self.move(card, c),
                )
                if column.id == card.stack_id:
                    button.add_style_class("active")
                    button.set_sensitive(False)
                moves.add(button)
            page.add(moves)

        done_label = "Mark as not done" if card.done else "Mark as done"
        done_button = Button(
            name="deck-done",
            child=Box(
                spacing=6,
                children=[
                    Image(icon_name="checkbox-checked-symbolic", icon_size=14),
                    Label(label=done_label),
                ],
            ),
            tooltip_text="Also moves it to the Done column" if not card.done else None,
            on_clicked=lambda *_: self.toggle_done(card),
        )
        if card.done:
            done_button.add_style_class("is-done")
        page.add(
            Box(
                name="deck-card-actions",
                spacing=6,
                h_align="end",
                children=[
                    Button(
                        name="deck-open-card",
                        label="Open in Deck",
                        on_clicked=lambda *_: self.open_web(card.board_id, card.id),
                    ),
                    done_button,
                ],
            )
        )

    def toggle_done(self, card: Card):
        self.status.set_label("Marking as not done…" if card.done else "Marking as done…")
        self.service.set_done(card, not card.done, on_done=self.on_write_done)

    def open_link(self, uri: str):
        run_async(["xdg-open", uri], capture=False)
        self.close()

    def move(self, card: Card, stack: Stack):
        self.stack_ids[card.board_id] = stack.id
        self.status.set_label(f"Moving to {stack.title}…")
        self.service.move_card(card, stack.id, on_done=self.on_write_done)

    # shared ----------------------------------------------------------------

    def card_row(self, card: Card, context: str | None = None, color: str | None = None) -> Button:
        title = Label(name="deck-card-row-title", label=card.title, h_align="start", ellipsization="end", max_chars_width=38)
        meta = Box(spacing=6)
        if card.due is not None:
            due = Box(
                name="deck-due",
                spacing=4,
                children=[Image(icon_name="alarm-symbolic", icon_size=12), Label(label=due_text(card.due))],
            )
            if card.overdue:
                due.add_style_class("overdue")
            meta.add(due)
        if context:
            meta.add(Label(name="deck-context", label=context, ellipsization="end", max_chars_width=26))
        for label in card.labels[:3]:
            meta.add(self.label_pill(label))
        if card.description.strip():
            meta.add(Image(name="deck-has-description", icon_name="emblem-documents-symbolic", icon_size=12))

        children = []
        if color:
            children.append(Box(name="deck-row-bar", style=f"background-color: {visible_accent(color)};"))
        body = Box(orientation="v", spacing=3, h_expand=True, children=[title])
        if meta.children:
            body.add(meta)
        children.append(body)
        row = Button(name="deck-card", child=Box(spacing=10, children=children), on_clicked=lambda *_: self.show_card(card))
        if card.done:
            row.add_style_class("done")
        return row

    @staticmethod
    def label_pill(label) -> Label:
        background = visible_accent(label.color)
        return Label(
            name="deck-label",
            label=label.title,
            style=f"background-color: {background}; color: {text_on(background)};",
        )

    @staticmethod
    def scrolled(child: Gtk.Widget, height: int = LIST_HEIGHT) -> ScrolledWindow:
        return ScrolledWindow(
            name="deck-scroller",
            child=child,
            h_scrollbar_policy="never",
            v_scrollbar_policy="automatic",
            propagate_width=True,
            propagate_height=True,
            max_content_size=(-1, height),
        )

    @staticmethod
    def empty(text: str) -> Box:
        return Box(
            name="deck-empty",
            orientation="v",
            spacing=6,
            children=[Image(icon_name="view-column-symbolic", icon_size=28), Label(label=text)],
        )


class DeckButton(Button):
    def __init__(self, **kwargs):
        super().__init__(
            name="deck-button",
            image=Image(icon_name="view-column-symbolic", icon_size=16),
            tooltip_text="Deck",
            **kwargs,
        )
