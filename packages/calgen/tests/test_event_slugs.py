"""Event permalink slugs.

The slug is a published URL: if it moves, the page loses whatever ranking and
inbound links it had, which defeats the point of having per-event pages at
all. These tests pin the shape and, more importantly, pin what the slug is
allowed to depend on.
"""
from calgen.event_utils import calculate_event_hash, event_slug, slugify


def _event(**kw):
    base = {
        'date': '2026-08-14',
        'time': '18:30',
        'title': 'DC Python Monthly Meetup',
        'url': 'https://www.meetup.com/dcpython/events/1/',
    }
    base.update(kw)
    base.setdefault('guid', calculate_event_hash(
        base['date'], base['time'], base['title'], base['url']))
    return base


class TestSlugify:
    def test_lowercases_and_hyphenates(self):
        assert slugify('DC Python Monthly Meetup') == 'dc-python-monthly-meetup'

    def test_strips_punctuation_without_leaving_runs(self):
        assert slugify('Rust & Go: Lightning Talks!!') == 'rust-go-lightning-talks'

    def test_transliterates_accents_rather_than_dropping_the_word(self):
        assert slugify('Café Coding Meetup') == 'cafe-coding-meetup'

    def test_no_leading_or_trailing_hyphens(self):
        s = slugify('  ...Hello World...  ')
        assert s == 'hello-world'

    def test_truncates_on_a_word_boundary(self):
        s = slugify('alpha bravo charlie delta echo foxtrot golf hotel india', max_length=20)
        assert len(s) <= 20
        assert not s.endswith('-')
        # cut between words, never mid-word
        assert s == 'alpha-bravo-charlie'

    def test_empty_input_is_empty_not_an_error(self):
        assert slugify('') == ''
        assert slugify(None) == ''


class TestEventSlug:
    def test_shape_is_date_title_guidprefix(self):
        e = _event()
        slug = event_slug(e)
        assert slug.startswith('2026-08-14-dc-python-monthly-meetup-')
        assert slug.endswith(e['guid'][:8])

    def test_is_stable_across_calls(self):
        e = _event()
        assert event_slug(e) == event_slug(e)

    def test_same_title_same_night_different_groups_do_not_collide(self):
        a = _event(title='Monthly Meetup', url='https://example.org/a')
        b = _event(title='Monthly Meetup', url='https://example.org/b')
        assert event_slug(a) != event_slug(b)

    def test_does_not_depend_on_presentation_fields(self):
        """prepare_events_by_day copies events and rewrites display fields.

        If the slug were computed from any of those, the same event would get
        one URL on the homepage and a different one on a month page.
        """
        plain = _event()
        decorated = _event()
        decorated.update({
            'display_title': 'DC Python Monthly Meetup (continuing)',
            'formatted_time': '6:30 pm',
            'permalink': '/events/whatever/',
            'group_website': 'https://example.org/',
        })
        assert event_slug(plain) == event_slug(decorated)

    def test_falls_back_to_a_computed_guid_when_absent(self):
        e = _event()
        del e['guid']
        assert event_slug(e).endswith(
            calculate_event_hash(e['date'], e['time'], e['title'], e['url'])[:8])

    def test_untitled_event_still_yields_a_usable_slug(self):
        e = _event(title='')
        slug = event_slug(e)
        assert slug.startswith('2026-08-14-')
        assert '--' not in slug

    def test_url_safe_characters_only(self):
        e = _event(title='Ünicode / Slashes & Spaces?')
        assert all(c.isalnum() or c == '-' for c in event_slug(e))
