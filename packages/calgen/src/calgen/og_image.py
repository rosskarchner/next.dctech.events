"""Social share card generation (next_dctech_events-409): one per event,
plus category pages, week pages, and /updates/ posts.

Every render_*_card() function takes plain fields in and returns a Pillow
Image, with no *implicit* filesystem/network access (site_dir, config, etc.
never enter this module) — specifically so it's fast to iterate on without a
real site build: see cli.py's `og-preview` command, which calls
render_event_card directly against sample data and writes a PNG you can open
immediately, and this module's own `__main__` block, which does the same
without even needing calgen installed as a console script. They all share
one skeleton (_render_card) — background, site label, fitted title, one
meta line, optional category icon — and differ only in what goes in the
title/meta line and whether an icon_path was resolved for them. An
icon_path is the one filesystem read in the module (a category icon PNG,
already resolved by the caller — see cli.py's og_images(), which knows
site_dir and therefore where site/static/images/categories/ lives); a card
rendered with icon_path=None touches no filesystem at all, same as before.

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

Category icons (site/static/images/categories/<slug>.png) are 64x64 pixel
art, same source the live site uses for the feed's category icon (see
main.css's .event-category-icon and events_by_day.html's category_icon_slugs
lookup) — scaled up here with NEAREST, never LANCZOS/BICUBIC: a smooth
resample blurs pixel art's hard edges, while NEAREST at a whole multiple of
the 64px source keeps every source pixel a crisp, uniformly-sized block.
"""
import os

from PIL import Image, ImageDraw, ImageFont

CARD_WIDTH = 1200
CARD_HEIGHT = 630

# Matches site/static/css/main.css's --color-primary / --color-primary-hover
# / --color-accent — same brand palette, not a separate one invented here.
BACKGROUND_COLOR = (41, 107, 107)      # #296b6b
BACKGROUND_COLOR_DARK = (30, 84, 84)   # #1e5454
ACCENT_COLOR = (186, 245, 188)         # #baf5bc
TEXT_COLOR = (255, 255, 255)
META_COLOR = (186, 245, 188)           # accent color doubles as the meta-text tint
ICON_BORDER_COLOR = (34, 35, 35)       # #222323 — matches the icon PNGs' own chip border

PADDING = 80
TITLE_MAX_LINES = 3
TITLE_FONT_SIZES = (72, 60, 50, 42, 36)  # tried largest-first; first that fits wins
TITLE_LINE_SPACING = 1.15
LABEL_FONT_SIZE = 30
META_FONT_SIZE = 34

# Icon sizes are whole multiples of the 64px source so NEAREST upscaling
# stays crisp (see module docstring). Event cards get a modest corner icon;
# category cards make it a hero element since the whole card is that one
# category.
EVENT_ICON_SIZE = 192   # 3x
CATEGORY_ICON_SIZE = 320  # 5x
ICON_MARGIN = 60

_LABEL_FONT_PATH = os.path.join(os.path.dirname(__file__), 'fonts', 'kenney-mini.ttf')


def _font(size):
    return ImageFont.load_default(size=size)


def _label_font(size):
    return ImageFont.truetype(_LABEL_FONT_PATH, size)


def _load_icon(icon_path, target_size):
    """Load a category icon PNG and scale it up cleanly to (approximately)
    target_size, preserving pixel-art edges — see module docstring. Returns
    an RGBA image whose actual side length is the nearest whole multiple of
    the source's width, plus a thin border matching the site's chip
    treatment (main.css's .event-category-icon border)."""
    source = Image.open(icon_path).convert('RGBA')
    scale = max(1, round(target_size / source.width))
    size = source.width * scale
    icon = source.resize((size, size), Image.NEAREST)

    bordered = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    bordered.paste(icon, (0, 0))
    border_width = max(2, size // 80)
    ImageDraw.Draw(bordered).rectangle(
        [(0, 0), (size - 1, size - 1)], outline=ICON_BORDER_COLOR, width=border_width)
    return bordered


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


def _render_card(title, meta_text, site_name='Tech Events', group_name=None,
                  icon_path=None, icon_size=EVENT_ICON_SIZE):
    """Shared skeleton behind every card kind — background, site label
    (+ optional group), fitted title, one meta line in the bottom band, and
    an optional category icon reserved on the right.

    title: shrunk/wrapped to fit, same as an event's own title.
    meta_text: whatever the bottom band should say — already fully composed
    (e.g. "Tuesday, September 1, 2026  ·  AI" or "12 upcoming events"); this
    function has no opinion on what belongs in it, matching how it already
    had none about date formatting.
    group_name: the organizing group's name, or None to omit it — set in
    the label row after site_name, in the portable default font (not Kenney
    Mini: it's event-submitted text, not fixed config text) so it reads as
    a distinct, secondary element rather than part of the brand mark.
    icon_path: path to a category icon PNG, or None to lay out the title at
    full width exactly as before — the one place this module touches the
    filesystem, and only when a caller resolved a path for it.
    """
    image = Image.new('RGB', (CARD_WIDTH, CARD_HEIGHT), color=BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    # A darker band along the bottom, not a flat block of one color end to
    # end — enough depth to read as designed rather than a placeholder fill.
    draw.rectangle(
        [(0, CARD_HEIGHT - 140), (CARD_WIDTH, CARD_HEIGHT)],
        fill=BACKGROUND_COLOR_DARK,
    )

    icon = _load_icon(icon_path, icon_size) if icon_path else None
    # icon.width, not the requested icon_size — _load_icon rounds to a clean
    # multiple of the 64px source, which may differ slightly.
    icon_reserved = (icon.width + ICON_MARGIN) if icon else 0
    content_width = CARD_WIDTH - 2 * PADDING - icon_reserved

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
            draw, f"  |  {group_name}", group_font,
            CARD_WIDTH - PADDING - icon_reserved - label_right)
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

    # Meta line in the bottom band.
    meta_font = _font(META_FONT_SIZE)
    meta_y = CARD_HEIGHT - 140 + (140 - META_FONT_SIZE) // 2 - 6
    draw.text((PADDING, meta_y), meta_text, font=meta_font, fill=META_COLOR)

    # Category icon, right-aligned and vertically centered in the same band
    # the title occupies.
    if icon:
        icon_x = CARD_WIDTH - PADDING - icon.width
        icon_y = top_bound + max(0, (bottom_bound - top_bound - icon.height) // 2)
        image.paste(icon, (icon_x, icon_y), icon)

    return image


def render_event_card(title, date_display, category_name=None, site_name='Tech Events',
                       group_name=None, icon_path=None):
    """Render one event's 1200x630 social share card.

    title: the event's own title, wrapped/shrunk to fit.
    date_display: already-formatted (e.g. "Tuesday, September 1, 2026") —
    this module has no opinion on date formatting, matching how
    routes/events.py's own _format_event_date already owns that elsewhere.
    category_name: the category's display name (not its slug), or None to
    omit the meta line's category segment entirely.
    group_name: see _render_card.
    icon_path: see _render_card. Resolved by the caller (cli.py), same
    "first category with an icon wins" policy as the live site's feed cards
    — not necessarily the same category as category_name, which is always
    the event's first category regardless of whether it has an icon.
    """
    meta_text = date_display
    if category_name:
        meta_text = f"{date_display}  ·  {category_name}"
    return _render_card(title, meta_text, site_name, group_name,
                         icon_path=icon_path, icon_size=EVENT_ICON_SIZE)


def render_category_card(category_name, event_count, site_name='Tech Events', icon_path=None):
    """Render a category page's card — the category itself as the title, an
    event count instead of a date/category meta line (it'd be redundant with
    the title here). icon_path: see _render_card — this category's own icon,
    shown larger than an event card's since the whole card is this one
    category."""
    meta_text = f"{event_count} upcoming event{'' if event_count == 1 else 's'}"
    return _render_card(category_name, meta_text, site_name,
                         icon_path=icon_path, icon_size=CATEGORY_ICON_SIZE)


def render_week_card(week_start_formatted, event_count, site_name='Tech Events'):
    """Render a week page's card. week_start_formatted: already-formatted,
    same convention as render_event_card's date_display."""
    title = f"Week of {week_start_formatted}"
    meta_text = f"{event_count} event{'' if event_count == 1 else 's'} this week"
    return _render_card(title, meta_text, site_name)


def render_post_card(title, date_formatted, site_name='Tech Events'):
    """Render an /updates/ post's card (free-form or weekly roundup — both
    share this same title-plus-date shape)."""
    return _render_card(title, date_formatted, site_name)


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
