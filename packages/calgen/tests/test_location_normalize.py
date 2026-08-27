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
    # Two genuinely different places, both outside the metro table so no state
    # correction applies.
    assert normalize_location('Portland, OR, Portland, ME') == \
        'Portland, OR, Portland, ME'


def test_a_repeat_that_only_matches_after_correction_still_collapses():
    # "Rockville, VA" is not a place. Correcting it to MD makes the two halves
    # identical, so correction has to run before the collapse.
    assert normalize_location('Rockville, MD, Rockville, VA') == 'Rockville, MD'


def test_correction_reaches_the_inner_pair_too():
    # The production case: correcting only the trailing pair would leave
    # "Arlington, DC, Arlington, VA", whose halves never match.
    assert normalize_location('Arlington, DC, Arlington, DC') == 'Arlington, VA'


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


# ── Impossible city/state pairs ────────────────────────────────────
# The site derives an event's region from the trailing state, so a wrong one
# files the event under the wrong region facet and a reader filtering by area
# never sees it (next_dctech_events-ubw). Within the DC metro a mismatch is a
# fact rather than a judgement, so it is corrected here rather than by a model.


@pytest.mark.parametrize('raw,expected', [
    # The case actually found in production, on a GDG-DC DevFest listing.
    ('3351 Fairfax Dr, 3351 Fairfax Drive, Arlington, DC, Arlington, DC',
     '3351 Fairfax Dr, 3351 Fairfax Drive, Arlington, VA'),
    ('Bethesda, VA', 'Bethesda, MD'),
    ('Rockville, DC', 'Rockville, MD'),
    ('Reston, MD', 'Reston, VA'),
    ('Silver Spring, VA', 'Silver Spring, MD'),
])
def test_an_impossible_metro_pair_is_corrected(raw, expected):
    assert normalize_location(raw) == expected


@pytest.mark.parametrize('clean', [
    'Arlington, VA', 'Bethesda, MD', 'Washington, DC', 'Annapolis, MD',
])
def test_a_correct_pair_is_untouched(clean):
    assert normalize_location(clean) == clean


@pytest.mark.parametrize('outside', [
    # Real places. Deciding these were meant to be local is a guess, and the
    # out-of-area pass is what should be making it.
    'Arlington, TX',
    'Arlington, MA',
    'Springfield, IL',
    'Boston, MA',
    'Somewhere, NY',
])
def test_a_city_outside_the_metro_is_left_alone(outside):
    assert normalize_location(outside) == outside


def test_an_unknown_city_is_left_alone():
    assert normalize_location('Nowheresville, VA') == 'Nowheresville, VA'


def test_correction_survives_the_doubling_collapse():
    # Both passes act on the same trailing segment; order must not matter.
    assert normalize_location('Fuse, 3351 Fairfax Dr, Arlington, DC, Arlington, DC') \
        == 'Fuse, 3351 Fairfax Dr, Arlington, VA'


def test_the_region_parser_now_reads_the_corrected_state():
    from calgen.location_utils import extract_location_info
    raw = 'Arlington, DC'
    assert extract_location_info(raw) == ('Washington', 'DC')
    assert extract_location_info(normalize_location(raw)) == ('Arlington', 'VA')
