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

Title/date/category text is Noto Sans (SIL OFL) with Noto Color Emoji
(SIL OFL) for emoji, loaded from a font directory (see _font_dir): Event
titles are free text from third-party feeds, and they really do contain
em dashes, accented letters, ®, and emoji — Pillow's embedded default font
(what this module used before) has none of those and drew a missing-glyph
box for each, which is how "Make with Notion — AI, APIs" went out with a
box in the middle. The fonts live OUTSIDE the calgen wheel (packages/calgen/
og-fonts/, copied into the CodeBuild source by build_lambdas.sh and found
through CALGEN_OG_FONT_DIR): the wheel is also installed into the Lambdas,
which never draw cards and should not carry an 11 MB emoji font.

Pillow has no per-glyph font fallback, so text is split into runs: emoji
clusters (a base, VS16, skin tone, ZWJ sequences, flags, keycaps) are drawn
from the emoji font's one bitmap size and scaled to the text size, and
everything else from Noto Sans. Anything neither font has (CJK, arrows, ...)
is replaced with a plain equivalent where one exists ("->") and otherwise
dropped, rather than drawn as a box. With no font directory at all (unit
tests, a bare `pip install calgen`) the same code falls back to Pillow's
embedded default font and the same cleanup, minus emoji. It is a single
regular weight — the card's visual hierarchy comes from size alone, not
weight, which is why title/meta font sizes are pushed further apart than a
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
import functools
import os
import re
import unicodedata

from PIL import Image, ImageDraw, ImageFont, features

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


_FONT_DIR_ENV = 'CALGEN_OG_FONT_DIR'
_TEXT_FONT_FILE = 'NotoSans-Regular.ttf'
_EMOJI_FONT_FILE = 'NotoColorEmoji.ttf'
# Noto Color Emoji is a CBDT bitmap font with exactly one strike, at 109 ppem;
# FreeType will not render it at any other size, so emoji are drawn at 109 and
# scaled. Each glyph is 136x128 at that size.
_EMOJI_STRIKE = 109
_EMOJI_ADVANCE = 136
_EMOJI_HEIGHT = 128
# Scale the emoji so it reads as slightly larger than a capital letter, the way
# browsers set it next to text, rather than at the strike's own 1.17em.
_EMOJI_SCALE = 0.92


def _font_dir():
    """Directory holding the card fonts, or None. CALGEN_OG_FONT_DIR wins;
    otherwise packages/calgen/og-fonts next to src/ when running from a
    checkout (editable install, `python -m calgen.og_image`)."""
    configured = os.environ.get(_FONT_DIR_ENV)
    if configured:
        return configured
    checkout = os.path.normpath(
        os.path.join(os.path.dirname(__file__), '..', '..', 'og-fonts'))
    return checkout if os.path.isdir(checkout) else None


def _font_path(filename):
    directory = _font_dir()
    path = os.path.join(directory, filename) if directory else None
    return path if path and os.path.isfile(path) else None


@functools.lru_cache(maxsize=None)
def _font(size):
    path = _font_path(_TEXT_FONT_FILE)
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


@functools.lru_cache(maxsize=1)
def _emoji_font():
    """The emoji font at its one bitmap size, or None if it is not installed."""
    path = _font_path(_EMOJI_FONT_FILE)
    return ImageFont.truetype(path, _EMOJI_STRIKE) if path else None


# Basic layout does no GSUB shaping, so a ZWJ sequence or a flag would draw as
# its parts side by side. Raqm (which needs libfribidi at runtime) joins them.
_HAS_RAQM = features.check('raqm')

_VS16 = '\ufe0f'
_ZWJ = '\u200d'
_SKIN = '\U0001F3FB-\U0001F3FF'
# BMP characters that are emoji by default (Emoji_Presentation=Yes)...
_BMP_EMOJI = ('\u231a\u231b\u23e9-\u23ec\u23f0\u23f3\u25fd\u25fe\u2614\u2615'
              '\u2648-\u2653\u267f\u2693\u26a1\u26aa\u26ab\u26bd\u26be\u26c4'
              '\u26c5\u26ce\u26d4\u26ea\u26f2\u26f3\u26f5\u26fa\u26fd\u2705'
              '\u270a\u270b\u2728\u274c\u274e\u2753-\u2755\u2757\u2795-\u2797'
              '\u27b0\u27bf\u2b1b\u2b1c\u2b50\u2b55')
# ...and ones that are text unless VS16 follows (a heart, (c), a sun).
_BMP_NEEDS_VS16 = ('\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u21aa\u231a-\u23ff'
                   '\u24c2\u25aa-\u25fe\u2600-\u27bf\u2934\u2935\u2b05-\u2b55'
                   '\u3030\u303d\u3297\u3299')
_EMOJI_ELEMENT = (f'(?:[\U0001F000-\U0001FAFF{_BMP_EMOJI}][{_SKIN}]?{_VS16}?'
                  f'|[{_BMP_NEEDS_VS16}]{_VS16})')
_EMOJI_CLUSTER = re.compile(
    '[\U0001F1E6-\U0001F1FF]{2}'                           # flag: two regional indicators
    '|[0-9#*]\ufe0f?\u20e3'                                  # keycap
    '|\U0001F3F4[\U000E0020-\U000E007E]+\U000E007F'        # subdivision flag (England...)
    f'|{_EMOJI_ELEMENT}(?:{_ZWJ}{_EMOJI_ELEMENT})*')         # emoji, optionally ZWJ-joined

# Characters Noto Sans lacks that have an honest plain-text equivalent, tried
# only when the active font has no glyph for the original. Anything not here
# (and not decomposable to something renderable) is dropped.
_REPLACEMENTS = {
    '\u2192': '->', '\u2190': '<-', '\u2194': '<->', '\u21d2': '=>', '\u21d0': '<=',
    '\u2713': 'v', '\u2714': 'v', '\u2717': 'x', '\u2718': 'x',
    '\u2605': '*', '\u2606': '*', '\u25cf': '*', '\u25cb': 'o',
    '\u2012': '-', '\u2013': '-', '\u2014': '-', '\u2015': '-', '\u2010': '-',
    '\u2011': '-', '\u2212': '-', '\u2022': '-', '\u2023': '-',
    '\u00a0': ' ', '\u2007': ' ', '\u2009': ' ', '\u202f': ' ', '\u3000': ' ',
}
# Invisible format characters: never drawn, never worth a box.
_INVISIBLE = re.compile('[\u00ad\u200b-\u200f\u2060-\u2064\ufeff\ufe0e\ufe0f]')

_not_defined_cache = {}


def _is_blank_glyph_font(font):
    """Mask bytes of the font's own "no such character" glyph (a box in Noto
    Sans and Pillow's default font), probed with a private-use codepoint."""
    key = ('notdef', id(font))
    if key not in _not_defined_cache:
        _not_defined_cache[key] = bytes(font.getmask('\ue000'))
    return _not_defined_cache[key]


def _has_glyph(font, ch):
    """True if font draws something real for ch. Whitespace counts as present
    (it is drawn as nothing on purpose)."""
    if ch.isspace():
        return True
    key = (id(font), ch)
    if key not in _not_defined_cache:
        _not_defined_cache[key] = bytes(font.getmask(ch)) != _is_blank_glyph_font(font)
    return _not_defined_cache[key]


def _emoji_tile(cluster, size):
    """The cluster drawn from the emoji font, scaled for text of `size` px:
    an RGBA image, or None if the font has no glyph for it."""
    return _emoji_tile_cached(cluster, size)


@functools.lru_cache(maxsize=512)
def _emoji_tile_cached(cluster, size):
    font = _emoji_font()
    if font is None:
        return None
    if not _HAS_RAQM:
        # No shaping: draw only the first element of a sequence, without the
        # VS16/keycap parts that would each take their own advance.
        cluster = re.sub('[\ufe0f\u20e3]', '', cluster.split(_ZWJ)[0])
    tile = Image.new('RGBA', (_EMOJI_ADVANCE + 40, _EMOJI_HEIGHT + 40), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((20, 20), cluster, font=font, embedded_color=True)
    if tile.getbbox() is None:
        return None  # glyph missing: the font draws nothing for it
    tile = tile.crop((20, 20, 20 + _EMOJI_ADVANCE, 20 + _EMOJI_HEIGHT))
    scale = size * _EMOJI_SCALE / _EMOJI_STRIKE
    return tile.resize((max(1, round(_EMOJI_ADVANCE * scale)),
                        max(1, round(_EMOJI_HEIGHT * scale))), Image.LANCZOS)


def _segments(text):
    """Split text into [('text', str) | ('emoji', cluster)] runs."""
    runs = []
    pos = 0
    for match in _EMOJI_CLUSTER.finditer(text):
        if match.start() > pos:
            runs.append(('text', text[pos:match.start()]))
        runs.append(('emoji', match.group()))
        pos = match.end()
    if pos < len(text):
        runs.append(('text', text[pos:]))
    return runs


def _clean_run(run, font):
    """Make a text run drawable with `font`: keep what it has a glyph for,
    otherwise a plain equivalent, otherwise nothing — never a missing-glyph box.
    Returns (text, changed): changed is True if anything was altered or dropped,
    which is what tells _prepare it may have left a doubled space behind."""
    cleaned = _INVISIBLE.sub('', run)
    changed = cleaned != run
    out = []
    for ch in cleaned:
        if _has_glyph(font, ch):
            out.append(ch)
            continue
        changed = True
        decomposed = ''.join(
            c for c in unicodedata.normalize('NFKD', ch)
            if not unicodedata.combining(c))
        if decomposed and all(_has_glyph(font, c) for c in decomposed):
            out.append(decomposed)
        elif _REPLACEMENTS.get(ch) is not None and all(
                _has_glyph(font, c) for c in _REPLACEMENTS[ch]):
            out.append(_REPLACEMENTS[ch])
    return ''.join(out), changed


def _prepare(text, font):
    """Text as it will actually be drawn: emoji kept (when the emoji font can
    draw them), everything else cleaned for `font`. Spacing is left exactly as
    written (the meta line's "  ·  " relies on it) unless something was dropped,
    in which case the gap it leaves is closed. If nothing of a non-empty string
    survives (an all-CJK title), the original comes back unchanged so the card
    shows boxes instead of silently saying nothing."""
    parts = []
    changed = False
    for kind, run in _segments(text):
        if kind == 'emoji':
            if _emoji_tile(run, font.size) is not None:
                parts.append(run)
            else:
                changed = True
        else:
            cleaned, run_changed = _clean_run(run, font)
            parts.append(cleaned)
            changed = changed or run_changed
    result = ''.join(parts)
    if changed:
        result = re.sub(r' {2,}', ' ', result)
    result = result.strip()
    return result if result or not text.strip() else text


_scratch_draw = ImageDraw.Draw(Image.new('L', (1, 1)))


def _run_advance(run, font):
    """Width of a text run. The ink's right edge (textbbox) ignores a trailing
    space, so an emoji after "Notion " would touch the word; the advance
    (getlength) includes it. Whichever is larger, so a run that is the whole
    string measures exactly as it always did."""
    return max(_scratch_draw.textbbox((0, 0), run, font=font)[2], round(font.getlength(run)))


def _text_width(text, font):
    """Rendered width of text, emoji included, in px."""
    width = 0
    for kind, run in _segments(text):
        tile = _emoji_tile(run, font.size) if kind == 'emoji' else None
        if tile is not None:
            width += tile.width
        elif kind == 'text':
            width += _run_advance(run, font)
    return width


def _draw_text(image, draw, xy, text, font, fill):
    """draw.text, except emoji clusters are pasted in from the emoji font."""
    x, y = xy
    ascent = font.getmetrics()[0]
    for kind, run in _segments(text):
        tile = _emoji_tile(run, font.size) if kind == 'emoji' else None
        if tile is not None:
            # Centre the emoji on the lowercase/cap-height band of the line.
            top = y + ascent - round(font.size * 0.36) - tile.height // 2
            image.paste(tile, (round(x), top), tile)
            x += tile.width
        elif kind == 'text':
            draw.text((x, y), run, font=font, fill=fill)
            x += _run_advance(run, font)


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
        if _text_width(candidate, font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _drop_last_unit(text):
    """text minus its last character, or its last whole emoji cluster (cutting
    a ZWJ sequence or flag in half would draw its pieces)."""
    for match in _EMOJI_CLUSTER.finditer(text):
        if match.end() == len(text):
            return text[:match.start()]
    return text[:-1]


def _truncate_to_width(draw, text, font, max_width):
    """Truncate text from the end, with an ellipsis, until it fits max_width.
    Text that already fits is returned unchanged."""
    if _text_width(text, font) <= max_width:
        return text
    while text and _text_width(text + '…', font) > max_width:
        text = _drop_last_unit(text)
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
    while last and _text_width(last + '…', font) > max_width:
        last = _drop_last_unit(last)
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
            draw, f"  |  {_prepare(group_name, group_font)}", group_font,
            CARD_WIDTH - PADDING - icon_reserved - label_right)
        _draw_text(image, draw, (label_right, PADDING), group_text, group_font, ACCENT_COLOR)

    # Title, vertically centered in the space between the label and the
    # meta band rather than pinned to a fixed y — a one-line title and a
    # three-line title should both look intentionally placed.
    title_font, lines = _fit_title(draw, _prepare(title, _font(TITLE_FONT_SIZES[0])),
                                   content_width)
    line_height = int(title_font.size * TITLE_LINE_SPACING)
    block_height = line_height * len(lines)
    top_bound = PADDING + LABEL_FONT_SIZE + 40
    bottom_bound = CARD_HEIGHT - 140 - 20
    title_top = top_bound + max(0, (bottom_bound - top_bound - block_height) // 2)
    for i, line in enumerate(lines):
        _draw_text(image, draw, (PADDING, title_top + i * line_height), line,
                   title_font, TEXT_COLOR)

    # Meta line in the bottom band.
    meta_font = _font(META_FONT_SIZE)
    meta_y = CARD_HEIGHT - 140 + (140 - META_FONT_SIZE) // 2 - 6
    _draw_text(image, draw, (PADDING, meta_y), _prepare(meta_text, meta_font),
               meta_font, META_COLOR)

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
