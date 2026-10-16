"""Keep user-chosen accent colours (Deck boards, labels, calendars) readable
on the bar's dark background."""

MIN_LUMINANCE = 0.18  # below this an accent disappears into the background
LIGHT_TEXT, DARK_TEXT = "#ECEFF4", "#2E3440"  # nord6, nord0


def _rgb(color: str) -> tuple[float, float, float]:
    value = color.lstrip("#")[:6]
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (0.5, 0.5, 0.5)


def _hex(rgb) -> str:
    return "#" + "".join(f"{round(c * 255):02x}" for c in rgb)


def luminance(color: str) -> float:
    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def visible_accent(color: str) -> str:
    """The colour itself if it shows up on the dark background, otherwise the
    same hue mixed toward white just far enough to."""
    rgb = _rgb(color)
    for step in range(11):
        t = step / 10
        mixed = tuple(c + (1 - c) * t for c in rgb)
        if luminance(_hex(mixed)) >= MIN_LUMINANCE:
            return _hex(mixed)
    return _hex(rgb)


def text_on(background: str) -> str:
    """Dark or light text, whichever reads better on `background`."""
    return DARK_TEXT if luminance(background) > 0.35 else LIGHT_TEXT
