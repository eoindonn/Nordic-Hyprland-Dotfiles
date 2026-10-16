"""Small Markdown to GTK renderer for card descriptions.

Block structure (headings, lists, task lists, quotes, code blocks, rules)
becomes separate labels; inline styling (bold, italic, strike, code, links)
becomes Pango markup. Covers what Deck's editor produces, not all of
CommonMark.
"""

import re
from collections.abc import Callable

from fabric.widgets.box import Box
from fabric.widgets.label import Label
from gi.repository import GLib, Gtk

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(?:\[([ xX])\]\s+)?(.*)$")
RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
QUOTE = re.compile(r"^\s*>\s?(.*)$")

INLINE_CODE = re.compile(r"`([^`]+)`")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
BARE_URL = re.compile(r"(?<![\"'=>])(https?://[^\s<]+[^\s<.,;:!?)\]])")
BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
ITALIC = re.compile(r"(?<![\w*])([*_])(?=\S)(.+?)(?<=\S)\1(?![\w*])")
STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


def inline(text: str) -> str:
    """Markdown inline syntax to Pango markup."""
    stash: list[str] = []

    def keep(markup: str) -> str:
        stash.append(markup)
        # private-use markers: GLib's C escaping would stop at a NUL
        return f"\ue000{len(stash) - 1}\ue001"

    # code spans and links first, so nothing inside them gets reformatted
    text = INLINE_CODE.sub(lambda m: keep(f"<tt>{GLib.markup_escape_text(m.group(1))}</tt>"), text)
    text = LINK.sub(
        lambda m: keep(
            f'<a href="{GLib.markup_escape_text(m.group(2))}">{GLib.markup_escape_text(m.group(1))}</a>'
        ),
        text,
    )
    text = BARE_URL.sub(lambda m: keep(f'<a href="{GLib.markup_escape_text(m.group(1))}">{GLib.markup_escape_text(m.group(1))}</a>'), text)
    text = GLib.markup_escape_text(text)
    text = BOLD.sub(r"<b>\2</b>", text)
    text = STRIKE.sub(r"<s>\1</s>", text)
    text = ITALIC.sub(r"<i>\2</i>", text)
    return re.sub("\ue000(\\d+)\ue001", lambda m: stash[int(m.group(1))], text)


def _label(markup: str, name: str, on_link: Callable[[str], None]) -> Label:
    label = Label(name=name, h_align="start", line_wrap="word-char")
    label.set_markup(markup)
    label.set_xalign(0)
    label.set_max_width_chars(44)
    label.set_selectable(True)
    label.connect("activate-link", lambda _, uri: (on_link(uri), True)[1])
    return label


def render(text: str, on_link: Callable[[str], None]) -> Box:
    """Build widgets for Markdown `text`; `on_link(uri)` opens clicked links."""
    box = Box(name="md", orientation="v", spacing=6)
    lines = text.replace("\r\n", "\n").split("\n")
    paragraph: list[str] = []

    def flush():
        if paragraph:
            # keep the author's line breaks; Deck's editor treats them as such
            box.add(_label("\n".join(inline(line.strip()) for line in paragraph), "md-p", on_link))
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if FENCE.match(line):
            flush()
            fence = FENCE.match(line).group(1)
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            block = Box(name="md-code")
            label = Label(name="md-code-text", h_align="start", line_wrap="char")
            label.set_markup(f"<tt>{GLib.markup_escape_text(chr(10).join(code))}</tt>")
            label.set_xalign(0)
            label.set_selectable(True)
            block.add(label)
            box.add(block)
        elif not line.strip():
            flush()
        elif m := HEADING.match(line):
            flush()
            level = len(m.group(1))
            box.add(_label(inline(m.group(2)), f"md-h{min(level, 3)}", on_link))
        elif RULE.match(line):
            flush()
            box.add(Gtk.Separator(name="md-rule"))
        elif m := QUOTE.match(line):
            flush()
            quote = [m.group(1)]
            while i + 1 < len(lines) and (q := QUOTE.match(lines[i + 1])):
                quote.append(q.group(1))
                i += 1
            box.add(Box(name="md-quote", children=[_label("\n".join(inline(q) for q in quote), "md-quote-text", on_link)]))
        elif m := LIST_ITEM.match(line):
            flush()
            indent, marker, task, content = m.groups()
            if task is not None:
                bullet = "☑" if task.lower() == "x" else "☐"
            elif marker[0].isdigit():
                bullet = marker
            else:
                bullet = "•"
            item = Box(name="md-li", spacing=8)
            item.set_margin_start(min(len(indent.expandtabs(4)) // 2, 4) * 12)
            bullet_label = Label(name="md-bullet", label=bullet, v_align="start")
            if task is not None and task.lower() == "x":
                bullet_label.add_style_class("checked")
            item.add(bullet_label)
            text_label = _label(inline(content), "md-li-text", on_link)
            if task is not None and task.lower() == "x":
                text_label.add_style_class("checked")
            text_label.set_hexpand(True)
            item.add(text_label)
            box.add(item)
        else:
            paragraph.append(line)
        i += 1
    flush()
    return box
