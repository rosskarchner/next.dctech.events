"""Per-event social share card generation (next_dctech_events-409).

render_event_card() is a pure function — event fields in, a Pillow Image
out, no filesystem/network access — specifically so it's fast to iterate on
without a real site build: see cli.py's `og-preview` command, which calls it
directly against sample data and writes a PNG you can open immediately, and
this module's own `__main__` block, which does the same without even
needing calgen installed as a console script.

Title/date/category text uses PIL.ImageFont.load_default(size=...) rather
than a bundled or system-installed TTF: that embedded font (Pillow >=10.1)
renders identically in local dev and in the CodeBuild environment that
actually runs `calgen og-images` in production, with zero font-file asset
management, and covers arbitrary event-submitted text (unicode, emoji)
that a small decorative font can't be relied on for. It is a single regular
weight — the card's visual hierarchy comes from size alone, not weight,
which is why title/meta font sizes are pushed further apart than a
bold/regular pairing would need.

The site-name label is the one exception: it's fixed, short, plain-ASCII
config text (not event-submitted), so it renders in the same Kenney Mini
pixel font as the real site's logo (site/static/css/main.css's .logo) for
brand consistency — bundled as fonts/kenney-mini.ttf, converted from
site/static/kenney-mini/kenney-mini.woff2 (Pillow can't load woff2
directly) via `fontTools.ttLib.TTFont(...).save()` with flavor=None. Kenney
fonts are CC0 (kenney.nl).
"""
import os

from PIL import Image, ImageDraw, ImageFont

CARD_WIDTH = 1200
CARD_HEIGHT = 630

# Matches site/static/css/main.css's --color-primary / --color-primary-hover
# / --color-accent — same brand palette, not a separate one invented here.
BACKGROUND_COLOR = (37, 99, 235)       # #2563eb
BACKGROUND_COLOR_DARK = (29, 78, 216)  # #1d4ed8
ACCENT_COLOR = (254, 243, 199)         # #fef3c7
TEXT_COLOR = (255, 255, 255)
META_COLOR = (254, 243, 199)           # accent color doubles as the meta-text tint

PADDING = 80
TITLE_MAX_LINES = 3
TITLE_FONT_SIZES = (72, 60, 50, 42, 36)  # tried largest-first; first that fits wins
TITLE_LINE_SPACING = 1.15
LABEL_FONT_SIZE = 30
META_FONT_SIZE = 34

_LABEL_FONT_PATH = os.path.join(os.path.dirname(__file__), 'fonts', 'kenney-mini.ttf')


def _font(size):
    return ImageFont.load_default(size=size)


def _label_font(size):
    return ImageFont.truetype(_LABEL_FONT_PATH, size)


def _wrap_text(draw, text, font, max_width):
    """Greedy word-wrap. Returns a list of lines; never splits a single word
    (an overlong word just overflows — real event titles don't have one)."""
    words = text.split()
    if not words:
        return []
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _truncate_to_width(draw, text, font, max_width):
    """Truncate text from the end, with an ellipsis, until it fits max_width.
    Text that already fits is returned unchanged."""
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text
    while text and draw.textbbox((0, 0), text + '…', font=font)[2] > max_width:
        text = text[:-1]
    return text.rstrip() + '…'


def _fit_title(draw, title, max_width):
    """Largest font size (from TITLE_FONT_SIZES) whose wrapped title fits
    within TITLE_MAX_LINES; falls back to the smallest with a truncated,
    ellipsized last line if even that overflows (defensive — no real event
    title has come close to needing it)."""
    for size in TITLE_FONT_SIZES:
        font = _font(size)
        lines = _wrap_text(draw, title, font, max_width)
        if len(lines) <= TITLE_MAX_LINES:
            return font, lines

    font = _font(TITLE_FONT_SIZES[-1])
    lines = _wrap_text(draw, title, font, max_width)[:TITLE_MAX_LINES]
    last = lines[-1]
    while last and draw.textbbox((0, 0), last + '…', font=font)[2] > max_width:
        last = last[:-1]
    lines[-1] = last.rstrip() + '…'
    return font, lines


def render_event_card(title, date_display, category_name=None, site_name='Tech Events',
                       group_name=None):
    """Render one 1200x630 social share card.

    title: the event's own title, wrapped/shrunk to fit.
    date_display: already-formatted (e.g. "Tuesday, September 1, 2026") —
    this module has no opinion on date formatting, matching how
    routes/events.py's own _format_event_date already owns that elsewhere.
    category_name: the category's display name (not its slug), or None to
    omit the meta line's category segment entirely.
    group_name: the organizing group's name, or None to omit it — set in
    the label row after site_name, in the portable default font (not Kenney
    Mini: it's event-submitted text, not fixed config text) so it reads as
    a distinct, secondary element rather than part of the brand mark.
    """
    image = Image.new('RGB', (CARD_WIDTH, CARD_HEIGHT), color=BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    # A darker band along the bottom, not a flat block of one color end to
    # end — enough depth to read as designed rather than a placeholder fill.
    draw.rectangle(
        [(0, CARD_HEIGHT - 140), (CARD_WIDTH, CARD_HEIGHT)],
        fill=BACKGROUND_COLOR_DARK,
    )

    content_width = CARD_WIDTH - 2 * PADDING

    # Site label, top-left — same Kenney Mini pixel font as the real site's
    # logo (site/static/css/main.css's .logo), for brand consistency.
    label_font = _label_font(LABEL_FONT_SIZE)
    label_text = site_name.upper()
    draw.text((PADDING, PADDING), label_text, font=label_font, fill=ACCENT_COLOR)

    # Organizing group, same line, portable font — "DC TECH EVENTS | DC
    # Rust". textbbox's right edge (computed from the label's own origin)
    # doubles as the group text's start x.
    if group_name:
        label_right = draw.textbbox((PADDING, PADDING), label_text, font=label_font)[2]
        group_font = _font(LABEL_FONT_SIZE)
        group_text = _truncate_to_width(
            draw, f"  |  {group_name}", group_font, CARD_WIDTH - PADDING - label_right)
        draw.text((label_right, PADDING), group_text, font=group_font, fill=ACCENT_COLOR)

    # Title, vertically centered in the space between the label and the
    # meta band rather than pinned to a fixed y — a one-line title and a
    # three-line title should both look intentionally placed.
    title_font, lines = _fit_title(draw, title, content_width)
    line_height = int(title_font.size * TITLE_LINE_SPACING)
    block_height = line_height * len(lines)
    top_bound = PADDING + LABEL_FONT_SIZE + 40
    bottom_bound = CARD_HEIGHT - 140 - 20
    title_top = top_bound + max(0, (bottom_bound - top_bound - block_height) // 2)
    for i, line in enumerate(lines):
        draw.text((PADDING, title_top + i * line_height), line, font=title_font, fill=TEXT_COLOR)

    # Meta line (date + category) in the bottom band.
    meta_font = _font(META_FONT_SIZE)
    meta_text = date_display
    if category_name:
        meta_text = f"{date_display}  ·  {category_name}"
    meta_y = CARD_HEIGHT - 140 + (140 - META_FONT_SIZE) // 2 - 6
    draw.text((PADDING, meta_y), meta_text, font=meta_font, fill=META_COLOR)

    return image


if __name__ == '__main__':
    # `python -m calgen.og_image` — a zero-setup preview independent of the
    # calgen CLI/click, for the fastest possible local iteration loop:
    # edit this module, rerun, look at preview.png.
    render_event_card(
        'Precision Raster Data for Scanning Tunneling Microscopes',
        'Thursday, August 27, 2026',
        'Hardware',
        group_name='DC Hardware Hackers',
    ).save('preview.png')
    print('Wrote preview.png')
