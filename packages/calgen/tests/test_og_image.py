"""Per-event social share cards (next_dctech_events-409).

render_event_card is a pure function (no filesystem/network), so these
exercise it directly rather than through a Flask route or a real build —
the point of that design was fast, dependency-free iteration, and these
tests get the same benefit.

Run: python -m pytest test_og_image.py
"""
from PIL import Image, ImageDraw

from calgen.og_image import (
    CARD_WIDTH, CARD_HEIGHT, TITLE_MAX_LINES, TITLE_FONT_SIZES,
    render_event_card, _wrap_text, _fit_title, _font,
)


def test_card_is_the_standard_og_image_size():
    card = render_event_card('Some Event', 'Monday, January 1, 2026', 'AI')
    assert card.size == (CARD_WIDTH, CARD_HEIGHT)


def test_short_title_uses_the_largest_font_size():
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    font, lines = _fit_title(draw, 'DC Python', CARD_WIDTH - 160)
    assert font.size == TITLE_FONT_SIZES[0]
    assert lines == ['DC Python']


def test_a_pathologically_long_title_is_truncated_with_an_ellipsis():
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    huge_title = ' '.join(['Word'] * 60)
    font, lines = _fit_title(draw, huge_title, CARD_WIDTH - 160)
    assert font.size == TITLE_FONT_SIZES[-1]
    assert len(lines) == TITLE_MAX_LINES
    assert lines[-1].endswith('…')
    # The truncated line, plus its ellipsis, must still fit the width budget
    # it was truncated for — the whole point of truncating in the first place.
    assert draw.textbbox((0, 0), lines[-1], font=font)[2] <= CARD_WIDTH - 160


def test_rendering_never_raises_across_a_range_of_real_shaped_titles():
    """Not a snapshot test (font rendering, hard to make robust across
    environments) — a smoke test that every realistic shape (empty-ish,
    unicode, emoji, punctuation-heavy) renders without error."""
    titles = [
        '',
        'A',
        'DC/PY: Intro to Rust 🦀',
        'Café Networking — für alle!',
        'AI & ML Meetup: LLMs, RAG, and Vector DBs (Beginner Friendly)',
        'x' * 200,
    ]
    for title in titles:
        card = render_event_card(title, 'Monday, January 1, 2026', 'AI')
        assert card.size == (CARD_WIDTH, CARD_HEIGHT)


def test_no_category_omits_the_category_and_its_separator():
    # render_event_card doesn't expose the meta string directly, so this
    # renders both variants and confirms they differ (the with-category
    # version draws more/different pixels) — the closest thing to an
    # observable assertion without OCR-ing the image.
    with_category = render_event_card('Event', 'Monday, January 1, 2026', 'AI')
    without_category = render_event_card('Event', 'Monday, January 1, 2026', None)
    assert with_category.tobytes() != without_category.tobytes()


def test_wrap_text_never_splits_a_single_word():
    font = _font(72)
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    # max_width=1 can't fit even two single-character words together, so
    # every resulting line should be exactly one word — confirming a single
    # word is never itself broken across lines.
    lines = _wrap_text(draw, 'a b c d e f g h', font, max_width=1)
    assert ''.join(lines).replace(' ', '') == 'abcdefgh'
    assert all(len(line.split()) == 1 for line in lines)


def test_wrap_text_on_empty_string_returns_no_lines():
    font = _font(48)
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    assert _wrap_text(draw, '', font, max_width=1000) == []
