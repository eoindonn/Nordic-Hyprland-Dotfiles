import re
from collections.abc import Callable
from dataclasses import dataclass

import cairo
from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.entry import Entry
from fabric.widgets.image import Image
from fabric.widgets.label import Label
from fabric.widgets.scrolledwindow import ScrolledWindow
from gi.repository import GdkPixbuf, GLib, Gtk
from loguru import logger

from ..services import bind
from ..system.clipboard import ClipboardStore
from ..utils import run_async
from .images import fit, rounded_surface
from .power import ConfirmButton

MAX_SHOWN = 40  # cards drawn at once; search still covers the whole history
THUMBNAIL_WIDTH = 300
THUMBNAIL_HEIGHT = 160
THUMBNAIL_RADIUS = 8
SEARCH_DELAY = 120  # ms

IMAGE_RE = re.compile(r"\[\[ binary data (\S+ \S+) (\w+) (\d+)x(\d+) \]\]")
COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
LINK_RE = re.compile(r"https?://\S+")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.\w+")
MASK = "•" * 12

KIND_ICONS = {
    "text": "text-x-generic-symbolic",
    "link": "insert-link-symbolic",
    "color": "applications-graphics-symbolic",
    "image": "image-x-generic-symbolic",
    "secret": "dialog-password-symbolic",
}

def looks_secret(text: str) -> bool:
    """Guess whether a copied string is a password or token.

    One "word" of 8-128 characters mixing at least three of lowercase,
    uppercase, digits and symbols. Hashes (hex only), plain words, numbers,
    links, email addresses and file paths don't qualify.
    """
    if not 8 <= len(text) <= 128 or any(c.isspace() for c in text):
        return False
    if LINK_RE.fullmatch(text) or EMAIL_RE.fullmatch(text) or text.startswith(("/", "~/")):
        return False
    classes = (
        any(c.islower() for c in text),
        any(c.isupper() for c in text),
        any(c.isdigit() for c in text),
        any(not c.isalnum() for c in text),
    )
    return sum(classes) >= 3


# decoded thumbnails, shared between the dropdowns on every monitor
_thumbnails: dict[str, cairo.ImageSurface] = {}


@dataclass
class ClipEntry:
    id: str
    line: str  # the raw `cliphist list` line, which `cliphist delete` expects
    text: str
    kind: str
    meta: str

    @classmethod
    def parse(cls, line: str) -> "ClipEntry":
        entry_id, _, text = line.partition("\t")
        if match := IMAGE_RE.fullmatch(text.strip()):
            size, fmt, width, height = match.groups()
            return cls(entry_id, line, text, "image", f"{fmt.upper()} · {width}×{height} · {size}")
        stripped = text.strip()
        if COLOR_RE.fullmatch(stripped):
            return cls(entry_id, line, stripped, "color", "Colour")
        if LINK_RE.fullmatch(stripped):
            return cls(entry_id, line, stripped, "link", "Link")
        if looks_secret(stripped):
            return cls(entry_id, line, stripped, "secret", f"Hidden · {len(stripped)} chars · hover to show")
        return cls(entry_id, line, text, "text", f"Text · {len(text)}{'+' if text.endswith('…') else ''} chars")

    def matches(self, query: str) -> bool:
        return query in self.text.lower() or query in self.meta.lower()


def rounded_thumbnail(data: bytes) -> cairo.ImageSurface:
    loader = GdkPixbuf.PixbufLoader()
    loader.write(data)
    loader.close()
    pixbuf = fit(loader.get_pixbuf(), THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
    return rounded_surface(pixbuf, THUMBNAIL_RADIUS)


class ClipboardHistory(Box):
    """Searchable card list of `cliphist` entries; click a card to copy it."""

    def __init__(self, close: Callable[[], None], store: ClipboardStore, **kwargs):
        self.close = close
        self.store = store
        self.entries: list[ClipEntry] = []
        self._search_id: int | None = None

        self.count_label = Label(name="clip-count")
        self.search = Entry(name="clip-search", placeholder="Search clipboard…", h_expand=True)
        self.search.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "edit-find-symbolic")
        self.search.connect("changed", lambda *_: self.schedule_render())
        self.search.connect("activate", lambda *_: self.copy_first_match())

        self.list_box = Box(name="clip-list", orientation="v", spacing=8)
        scroller = ScrolledWindow(
            name="clip-scroller",
            child=self.list_box,
            h_scrollbar_policy="never",
            v_scrollbar_policy="automatic",
            propagate_width=True,
            propagate_height=True,
            max_content_size=(-1, 460),
        )

        super().__init__(
            name="clipboard",
            orientation="v",
            spacing=10,
            children=[
                Box(
                    name="clip-header",
                    spacing=8,
                    children=[
                        Label(name="clip-title", label="Clipboard"),
                        self.count_label,
                        Box(h_expand=True),
                        ConfirmButton(
                            ["cliphist", "wipe"],
                            "Clear all",
                            on_exit=lambda _: self.store.reload(),
                            name="clip-clear",
                            label="Clear all",
                            armed_label="Confirm clear?",
                        ),
                    ],
                ),
                self.search,
                scroller,
            ],
            **kwargs,
        )

        # cards are built in the background as the clipboard changes, so
        # opening the dropdown has nothing left to do
        bind(self, store, "changed", lambda *_: self.on_store_changed())
        self.on_store_changed()

    # -- dropdown hooks ------------------------------------------------------

    def on_close(self):
        self.search.set_text("")

    # -- data ----------------------------------------------------------------

    def on_store_changed(self):
        self.entries = [ClipEntry.parse(line) for line in self.store.lines]
        live_ids = {entry.id for entry in self.entries}
        for stale in _thumbnails.keys() - live_ids:
            del _thumbnails[stale]
        self.render()

    def schedule_render(self):
        if self._search_id is not None:
            GLib.source_remove(self._search_id)
        self._search_id = GLib.timeout_add(SEARCH_DELAY, self._on_search_timeout)

    def _on_search_timeout(self):
        self._search_id = None
        self.render()
        return False

    def matching(self) -> list[ClipEntry]:
        query = self.search.get_text().strip().lower()
        return [e for e in self.entries if not query or e.matches(query)]

    def render(self):
        for child in self.list_box.children:
            child.destroy()

        matches = self.matching()
        total = len(self.entries)
        self.count_label.set_label(
            f"{len(matches)} of {total}" if len(matches) != total else f"{total} items"
        )

        if not matches:
            self.list_box.add(self.empty_state(bool(self.entries)))
        for entry in matches[:MAX_SHOWN]:
            self.list_box.add(self.make_card(entry))
        self.list_box.show_all()

    # -- widgets -------------------------------------------------------------

    @staticmethod
    def empty_state(searching: bool) -> Box:
        return Box(
            name="clip-empty",
            orientation="v",
            spacing=8,
            children=[
                Image(icon_name="edit-paste-symbolic", icon_size=40),
                Label(label="No matches" if searching else "Clipboard is empty"),
            ],
        )

    def make_card(self, entry: ClipEntry) -> Box:
        meta = Box(
            name="clip-meta",
            spacing=6,
            children=[
                Image(icon_name=KIND_ICONS[entry.kind], icon_size=14),
                Label(label=entry.meta, h_align="start"),
            ],
        )

        if entry.kind == "image":
            preview = self.make_thumbnail(entry)
        elif entry.kind == "color":
            preview = Box(
                spacing=10,
                children=[
                    Box(name="clip-swatch", style=f"background-color: {entry.text};"),
                    Label(name="clip-text", label=entry.text),
                ],
            )
        else:
            preview = Label(name="clip-text", label=entry.text.strip(), line_wrap="word-char", ellipsization="end")
            preview.set_lines(3)
            preview.set_xalign(0)
            preview.set_max_width_chars(40)

        body = Button(
            name="clip-body",
            h_expand=True,
            child=Box(orientation="v", spacing=6, children=[meta, preview]),
            on_clicked=lambda *_: self.copy(entry),
        )
        if entry.kind == "secret":
            # reveal only while the pointer is over this card
            preview.set_label(MASK)
            body.connect("enter-notify-event", lambda *_: preview.set_label(entry.text))
            body.connect("leave-notify-event", lambda *_: preview.set_label(MASK))
        delete = Button(
            name="clip-delete",
            v_align="start",
            image=Image(icon_name="window-close-symbolic", icon_size=12),
            tooltip_text="Remove",
            on_clicked=lambda *_: self.delete(entry),
        )
        card = Box(name="clip-card", children=[body, delete])
        card.add_style_class(entry.kind)
        return card

    def make_thumbnail(self, entry: ClipEntry) -> Image:
        image = Image(name="clip-thumbnail", h_align="start")
        if (surface := _thumbnails.get(entry.id)) is not None:
            image.set_from_surface(surface)
            return image

        def on_decoded(data: bytes):
            try:
                _thumbnails[entry.id] = rounded_thumbnail(data)
            except GLib.Error as e:
                logger.warning(f"failed loading clipboard image {entry.id}: {e.message}")
                return
            image.set_from_surface(_thumbnails[entry.id])

        run_async(["cliphist", "decode", entry.id], on_decoded)
        return image

    # -- actions -------------------------------------------------------------

    def copy(self, entry: ClipEntry):
        run_async(
            ["cliphist", "decode", entry.id],
            lambda data: run_async(["wl-copy"], stdin=data, capture=False),
        )
        self.close()

    def copy_first_match(self):
        if matches := self.matching():
            self.copy(matches[0])

    def delete(self, entry: ClipEntry):
        self.store.delete(entry.line)
        self.entries.remove(entry)
        _thumbnails.pop(entry.id, None)
        self.render()


class ClipboardButton(Button):
    def __init__(self, **kwargs):
        super().__init__(
            name="clipboard-button",
            label="\U000f014d",
            **kwargs,
        )
