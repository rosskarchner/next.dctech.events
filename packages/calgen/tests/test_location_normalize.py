"""Tidying the location strings feeds hand us.

Meetup's iCal LOCATION already ends in "City, ST" and then appends the city and
state again, so most imported events displayed them twice
(next_dctech_events-8so). Machine-generated noise across every Meetup-sourced
group, so it is fixed at parse time rather than by a moderator or weekly by the
QC agent.
"""
import pytest

from calgen.location_utils import normalize_location


# The five shapes actually found in the queue on 2026-08-26.
@pytest.mark.parametrize('raw,expected', [
    ('Rockville Memorial Library, Rockville Town Square Plaza, 21 Maryland Ave, '
     'Rockville, MD, Rockville, MD',
     'Rockville Memorial Library, Rockville Town Square Plaza, 21 Maryland Ave, '
     'Rockville, MD'),
    ('Fuse At Mason Square, 3351 Fairfax Drive,, Arlington, VA, Arlington, VA',
     'Fuse At Mason Square, 3351 Fairfax Drive, Arlington, VA'),
    ('Barbara M. Donnellan Auditorium, 1015 N Quincy St., Arlington, va, '
     'Arlington, va',
     'Barbara M. Donnellan Auditorium, 1015 N Quincy St., Arlington, VA'),
    ('HacDC, 3333 14th St NW, Suite M110, Washington, DC, Washington, DC',
     'HacDC, 3333 14th St NW, Suite M110, Washington, DC'),
    ("Steve's Place, Please ask for address, Silver Spring, MD, Silver Spring, MD",
     "Steve's Place, Please ask for address, Silver Spring, MD"),
])
def test_the_production_cases(raw, expected):
    assert normalize_location(raw) == expected


@pytest.mark.parametrize('clean', [
    'Washington, DC',
    'Army Navy Country Club, Arlington, VA',
    'Compass Pointe Golf Club, Pasadena, MD',
    'HacDC, 3333 14th St NW, Suite M110, Washington, DC',
])
def test_an_already_clean_location_is_untouched(clean):
    assert normalize_location(clean) == clean


def test_empty_and_missing_locations_survive():
    assert normalize_location('') == ''
    assert normalize_location(None) is None
    assert normalize_location(',,,') == ''


def test_a_trailing_state_is_upper_cased():
    # Meetup emits a lowercase one often enough that it shows on the site.
    assert normalize_location('Arlington, va') == 'Arlington, VA'


def test_something_that_is_not_a_state_is_left_alone():
    # Two letters, but not a state — do not shout it.
    assert normalize_location('Hackerspace, m1') == 'Hackerspace, m1'


def test_a_triple_repeat_collapses():
    assert normalize_location(
        '21 Maryland Ave, Rockville, MD, Rockville, MD, Rockville, MD'
    ) == '21 Maryland Ave, Rockville, MD'


def test_a_venue_that_genuinely_repeats_a_word_keeps_it():
    # Only an exact repeat of the final *two* segments is dropped.
    assert normalize_location('Arlington Arts Center, Arlington, VA') == \
        'Arlington Arts Center, Arlington, VA'


def test_a_near_repeat_is_not_collapsed():
    assert normalize_location('Rockville, MD, Rockville, VA') == \
        'Rockville, MD, Rockville, VA'


def test_only_the_end_is_collapsed():
    # A repeat in the middle is someone's actual address formatting.
    raw = 'Rockville, MD, Rockville, MD, 21 Maryland Ave'
    assert normalize_location(raw) == raw


def test_the_region_parser_still_agrees_after_normalizing():
    # extract_location_info reads the trailing "City, ST"; normalizing must not
    # move what it finds.
    from calgen.location_utils import extract_location_info
    raw = ('Barbara M. Donnellan Auditorium, 1015 N Quincy St., Arlington, va, '
           'Arlington, va')
    assert extract_location_info(raw) == extract_location_info(
        normalize_location(raw))
