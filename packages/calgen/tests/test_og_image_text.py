"""Text on social cards: dashes, accents, symbols and emoji.

Cards used to draw with Pillow's embedded default font, which has none of em
dashes, accented letters, (R), or emoji, so each came out as a missing-glyph box
("Make with Notion [box] AI, APIs & Developer Platform"). Now Noto Sans draws
text and Noto Color Emoji draws emoji (og_image.py's docstring has the reasoning);
anything neither font has is replaced or dropped instead of boxed.

Most tests need the real fonts in packages/calgen/og-fonts/, found automatically
from a checkout. The fallback tests run without them on purpose.
"""
import pytest
from PIL import Image, ImageDraw

from calgen import og_image
from calgen.og_image import (
    CARD_WIDTH, TITLE_FONT_SIZES,
    render_event_card, _prepare, _segments, _font, _text_width, _wrap_text,
    _truncate_to_width, _drop_last_unit, _emoji_tile, _has_glyph,
)

HAVE_FONTS = (og_image._font_path(og_image._TEXT_FONT_FILE) is not None
              and og_image._font_path(og_image._EMOJI_FONT_FILE) is not None)
needs_fonts = pytest.mark.skipif(not HAVE_FONTS, reason='og-fonts/ not available')


def _clear_caches():
    og_image._font.cache_clear()
    og_image._emoji_font.cache_clear()
    og_image._emoji_tile_cached.cache_clear()
    og_image._not_defined_cache.clear()


@pytest.fixture
def no_fonts(monkeypatch, tmp_path):
    """No font directory: Pillow's embedded default font, no emoji."""
    monkeypatch.setenv(og_image._FONT_DIR_ENV, str(tmp_path))
    _clear_caches()
    yield
    monkeypatch.undo()
    _clear_caches()


def _colored_pixels(image):
    """Pixels more saturated than anything else on a card: emoji. The teal
    background, its darker band and the mint text all stay under a spread of 70
    between their strongest and weakest channel; white is 0."""
    raw = image.convert('RGB').tobytes()
    triples = zip(raw[0::3], raw[1::3], raw[2::3])
    return sum(1 for r, g, b in triples if max(r, g, b) - min(r, g, b) > 100)


# ── the bug as reported ──────────────────────────────────────────────

@needs_fonts
def test_the_notion_title_keeps_its_em_dash():
    title = 'Make with Notion — AI, APIs & Developer Platform'
    assert _prepare(title, _font(60)) == title


@needs_fonts
@pytest.mark.parametrize('title', [
    'SOC Analyst Prep — Live Workshop | October 2026',
    'Civic Hack DC 2027 – Part 2: From Prototype to Production',
    'Succeed In a Skills-Based Hiring World With A CMF® and CMLF®',
    'Café Networking für alle',
    '€5 entry, § 3',
])
def test_real_world_punctuation_and_accents_are_drawn_not_boxed(title):
    font = _font(60)
    assert all(_has_glyph(font, ch) for ch in title)
    assert _prepare(title, font) == title


# ── emoji ─────────────────────────────────────────────────────────────

@needs_fonts
def test_an_emoji_title_draws_color_pixels():
    plain = render_event_card('Side Projects and Networking', 'Monday', 'AI')
    emoji = render_event_card('⚡️ Side Projects and Networking', 'Monday', 'AI')
    assert _colored_pixels(plain) == 0
    assert _colored_pixels(emoji) > 500


@needs_fonts
def test_emoji_in_the_group_name_and_meta_line_are_drawn_too():
    base = render_event_card('Event', 'Monday', 'AI', group_name='Craft Night')
    group = render_event_card('Event', 'Monday', 'AI', group_name='Craft Night \U0001F9F6')
    meta = render_event_card('Event', 'Monday \U0001F9F6', 'AI')
    assert _colored_pixels(group) > _colored_pixels(base) + 100
    assert _colored_pixels(meta) > 100


@needs_fonts
def test_an_emoji_takes_horizontal_room():
    font = _font(60)
    assert _text_width('\U0001F9F6', font) > 30
    assert _text_width('a \U0001F9F6', font) > _text_width('a', font) + 30


@needs_fonts
def test_a_space_before_an_emoji_is_kept_as_space():
    # textbbox ignores a trailing space, which would glue the emoji to the word.
    font = _font(60)
    with_space = _text_width('Notion \U0001F9F6', font)
    without = _text_width('Notion\U0001F9F6', font)
    assert with_space > without + 5


# ── splitting text from emoji ─────────────────────────────────────────

def _kinds(text):
    return [(k, r) for k, r in _segments(text)]


def test_plain_text_is_one_text_run():
    assert _kinds('Hello, World') == [('text', 'Hello, World')]


def test_an_emoji_between_words_splits_into_three_runs():
    assert _kinds('Hi \U0001F9F6 there') == [
        ('text', 'Hi '), ('emoji', '\U0001F9F6'), ('text', ' there')]


def test_a_variation_selector_stays_with_its_emoji():
    assert _kinds('⚡️ Side') == [('emoji', '⚡️'), ('text', ' Side')]
    assert _kinds('\U0001F5A5️') == [('emoji', '\U0001F5A5️')]


def test_zwj_sequences_flags_keycaps_and_skin_tones_are_single_clusters():
    technologist = '\U0001F468‍\U0001F4BB'
    flag = '\U0001F1FA\U0001F1F8'
    thumbs = '\U0001F44D\U0001F3FD'
    keycap = '1️⃣'
    for cluster in (technologist, flag, thumbs, keycap):
        assert _kinds(f'a{cluster}b') == [('text', 'a'), ('emoji', cluster), ('text', 'b')]


def test_symbols_that_are_text_by_default_are_not_emoji():
    # Digits, (c), (R), TM and a bare heart are text unless VS16 asks otherwise.
    for text in ('2026', '© 2026', 'CMF®', 'Brand™', 'I ❤ DC'):
        assert all(kind == 'text' for kind, _ in _segments(text)), text
    assert _kinds('❤️') == [('emoji', '❤️')]


def test_a_lone_regional_indicator_is_not_a_flag():
    assert [k for k, _ in _segments('\U0001F1FA')] == ['emoji']  # a single glyph, not a pair


def test_drop_last_unit_removes_a_whole_cluster_not_half_of_it():
    technologist = '\U0001F468‍\U0001F4BB'
    assert _drop_last_unit('Hi ' + technologist) == 'Hi '
    assert _drop_last_unit('Hi ' + '\U0001F1FA\U0001F1F8') == 'Hi '
    assert _drop_last_unit('Hi') == 'H'


@needs_fonts
def test_truncating_never_cuts_an_emoji_sequence_in_half():
    font = _font(30)
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    text = 'Word ' * 12 + '\U0001F468‍\U0001F4BB'
    for width in range(60, 500, 37):
        out = _truncate_to_width(draw, text, font, width)
        assert '‍' not in out or '\U0001F468‍\U0001F4BB' in out
        assert _text_width(out, font) <= width or out == '…'


@needs_fonts
def test_wrapping_counts_emoji_width():
    font = _font(TITLE_FONT_SIZES[0])
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    plain = _wrap_text(draw, 'one two three four', font, CARD_WIDTH - 160)
    padded = _wrap_text(draw, 'one two three four ' + '\U0001F9F6 ' * 8, font, CARD_WIDTH - 160)
    assert len(padded) > len(plain)


# ── what neither font has ─────────────────────────────────────────────

@needs_fonts
def test_an_arrow_noto_sans_lacks_becomes_a_plain_equivalent():
    assert _prepare('A → B', _font(60)) == 'A -> B'


@needs_fonts
def test_characters_with_no_equivalent_are_dropped_and_the_gap_closed():
    assert _prepare('A 日本 B', _font(60)) == 'A B'


@needs_fonts
def test_a_title_with_nothing_drawable_comes_back_unchanged_not_blank():
    assert _prepare('日本語', _font(60)) == '日本語'


def test_spacing_is_left_alone_when_nothing_is_dropped():
    # The meta line's separator is two spaces either side of the dot.
    meta = 'Tuesday, October 6, 2026  ·  Software Development'
    assert _prepare(meta, _font(34)) == meta


def test_empty_and_blank_text_is_fine():
    assert _prepare('', _font(34)) == ''
    assert _prepare('   ', _font(34)) == ''


@needs_fonts
def test_invisible_format_characters_are_removed():
    assert _prepare('Hello​World­!', _font(60)) == 'HelloWorld!'


# ── no fonts installed: degrade, don't box ────────────────────────────

def test_without_fonts_a_dash_and_accents_still_never_become_boxes(no_fonts):
    font = _font(60)
    out = _prepare('Café — für alle \U0001F9F6 → B', font)
    assert out == 'Cafe - fur alle -> B'
    assert all(_has_glyph(font, ch) for ch in out)


def test_without_fonts_cards_still_render(no_fonts):
    card = render_event_card('Make with Notion — AI \U0001F9F6', 'Monday', 'AI',
                             group_name='Notion DC')
    assert card.size[0] == CARD_WIDTH
    assert _colored_pixels(card) == 0  # no emoji font, so no emoji


def test_without_an_emoji_font_there_is_no_tile(no_fonts):
    assert _emoji_tile('\U0001F9F6', 60) is None


# ── without libraqm (no GSUB shaping) ─────────────────────────────────

@needs_fonts
def test_without_raqm_a_zwj_sequence_draws_one_glyph_not_its_parts(monkeypatch):
    monkeypatch.setattr(og_image, '_HAS_RAQM', False)
    og_image._emoji_tile_cached.cache_clear()
    try:
        tile = _emoji_tile('\U0001F468‍\U0001F4BB', 60)
        assert tile is not None
        single = _emoji_tile('\U0001F468', 60)
        assert tile.width == single.width
    finally:
        monkeypatch.undo()
        og_image._emoji_tile_cached.cache_clear()


@needs_fonts
def test_a_glyph_the_emoji_font_lacks_is_dropped(monkeypatch):
    # U+1F000 (mahjong) is inside the emoji range but not in the font; it draws
    # blank, and a blank tile must read as "missing", not as a zero-ink emoji.
    assert _emoji_tile('\U0001F000', 60) is None
    assert _prepare('a \U0001F000 b', _font(60)) == 'a b'
