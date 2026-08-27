#!/usr/bin/env python3
import usaddress

# Full US state name → abbreviation map
_STATE_NAME_TO_ABBR = {
    'ALABAMA': 'AL', 'ALASKA': 'AK', 'ARIZONA': 'AZ', 'ARKANSAS': 'AR',
    'CALIFORNIA': 'CA', 'COLORADO': 'CO', 'CONNECTICUT': 'CT', 'DELAWARE': 'DE',
    'FLORIDA': 'FL', 'GEORGIA': 'GA', 'HAWAII': 'HI', 'IDAHO': 'ID',
    'ILLINOIS': 'IL', 'INDIANA': 'IN', 'IOWA': 'IA', 'KANSAS': 'KS',
    'KENTUCKY': 'KY', 'LOUISIANA': 'LA', 'MAINE': 'ME', 'MARYLAND': 'MD',
    'MASSACHUSETTS': 'MA', 'MICHIGAN': 'MI', 'MINNESOTA': 'MN', 'MISSISSIPPI': 'MS',
    'MISSOURI': 'MO', 'MONTANA': 'MT', 'NEBRASKA': 'NE', 'NEVADA': 'NV',
    'NEW HAMPSHIRE': 'NH', 'NEW JERSEY': 'NJ', 'NEW MEXICO': 'NM', 'NEW YORK': 'NY',
    'NORTH CAROLINA': 'NC', 'NORTH DAKOTA': 'ND', 'OHIO': 'OH', 'OKLAHOMA': 'OK',
    'OREGON': 'OR', 'PENNSYLVANIA': 'PA', 'RHODE ISLAND': 'RI', 'SOUTH CAROLINA': 'SC',
    'SOUTH DAKOTA': 'SD', 'TENNESSEE': 'TN', 'TEXAS': 'TX', 'UTAH': 'UT',
    'VERMONT': 'VT', 'VIRGINIA': 'VA', 'WASHINGTON': 'WA', 'WEST VIRGINIA': 'WV',
    'WISCONSIN': 'WI', 'WYOMING': 'WY', 'DISTRICT OF COLUMBIA': 'DC',
    'PUERTO RICO': 'PR', 'VIRGIN ISLANDS': 'VI', 'GUAM': 'GU',
}

# Abbreviation → display name map
_STATE_ABBR_TO_NAME = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'Washington DC',
    'PR': 'Puerto Rico', 'VI': 'Virgin Islands', 'GU': 'Guam',
    'MP': 'Northern Mariana Islands', 'AS': 'American Samoa',
}


# Which state each DC-metro city is actually in. Scoped to the metro on
# purpose: within it a mismatch is a fact rather than a judgement, and outside
# it the same city name is often ambiguous (Arlington is in VA here, and in TX
# and MA elsewhere). A feed saying "Arlington, DC" is stating something
# impossible — DC has no Arlington — and it silently files the event under the
# wrong region facet, because extract_location_info reads the trailing state
# (next_dctech_events-ubw).
_DC_METRO_CITY_STATE = {
    'arlington': 'VA', 'alexandria': 'VA', 'reston': 'VA', 'tysons': 'VA',
    'mclean': 'VA', 'vienna': 'VA', 'herndon': 'VA', 'ashburn': 'VA',
    'sterling': 'VA', 'fairfax': 'VA', 'falls church': 'VA', 'chantilly': 'VA',
    'fredericksburg': 'VA', 'manassas': 'VA', 'leesburg': 'VA',
    'springfield': 'VA', 'annandale': 'VA', 'centreville': 'VA',
    'bethesda': 'MD', 'silver spring': 'MD', 'rockville': 'MD',
    'college park': 'MD', 'greenbelt': 'MD', 'gaithersburg': 'MD',
    'hanover': 'MD', 'pasadena': 'MD', 'annapolis': 'MD', 'laurel': 'MD',
    'bowie': 'MD', 'hyattsville': 'MD',
    'washington': 'DC',
}

# Only a swap *within* the metro's three states is corrected. If a feed claims
# "Arlington, TX" we leave it — that is a real place, and deciding it was meant
# to be Virginia is a guess the out-of-area pass should make, not this.
_DC_METRO_STATES = frozenset({'DC', 'MD', 'VA'})


def correct_improbable_state(city, state):
    """The state a DC-metro city is really in, or `state` unchanged.

    Returns the input untouched unless the city is known *and* the claimed
    state is one of the metro's own — so a genuinely out-of-area listing is
    left for a human or the QC agent to judge.
    """
    if not city or not state:
        return state
    known = _DC_METRO_CITY_STATE.get(str(city).strip().casefold())
    claimed = str(state).strip().upper()
    if known and claimed in _DC_METRO_STATES and claimed != known:
        return known
    return state


def normalize_location(location):
    """Tidy a location string from a feed.

    Two defects, both machine-generated and both spanning whole platforms
    rather than being one organiser's typo, so both are fixed once here instead
    of by a moderator or weekly by the QC agent.

    Meetup's iCal LOCATION already ends in "City, ST" and then appends the city
    and state again, so a majority of imported events displayed them twice
    (next_dctech_events-8so):

        Rockville Memorial Library, Rockville Town Square Plaza,
        21 Maryland Ave, Rockville, MD, Rockville, MD

    And a DC-metro city is sometimes paired with a state it is not in —
    "Arlington, DC", which is impossible, DC has no Arlington. That one matters
    beyond looking wrong: the site derives an event's region from the trailing
    state, so it files the event under the wrong region facet and a reader
    filtering by area never sees it (next_dctech_events-ubw).

    Every "City, ST" pair is corrected, not just the trailing one, and only
    then is a repeat collapsed. Correcting the tail alone is not enough — the
    production case was "Arlington, DC, Arlington, DC", whose halves only match
    once *both* are fixed. Collapsing first is not enough either, because
    "Rockville, MD, Rockville, VA" does not match until the impossible VA
    becomes MD.

    Deliberately conservative in both passes: only an exact repeat of the final
    two segments is dropped and only from the end, so a venue that genuinely
    repeats a word keeps it; and only a swap within the metro's own states is
    corrected, so "Arlington, TX" is left as the real place it is.
    """
    if not location:
        return location

    parts = [p.strip() for p in str(location).split(',')]
    parts = [p for p in parts if p]
    if not parts:
        return ''

    # Upper-case and sanity-check every state segment. Meetup emits a lowercase
    # one often enough ("Arlington, va") that it showed on the site.
    for i in range(1, len(parts)):
        if len(parts[i]) != 2 or parts[i].upper() not in _STATE_ABBR_TO_NAME:
            continue
        parts[i] = parts[i].upper()
        parts[i] = correct_improbable_state(parts[i - 1], parts[i])

    # "…, Rockville, MD, Rockville, MD" -> "…, Rockville, MD". Looped so a
    # triple collapses too.
    while len(parts) >= 4:
        if [p.casefold() for p in parts[-2:]] != \
           [p.casefold() for p in parts[-4:-2]]:
            break
        parts = parts[:-2]

    return ', '.join(parts)


def extract_location_info(address):
    """
    Extract city and state from an address string.

    Uses usaddress library to parse addresses and extract PlaceName and StateName.
    Handles multi-word cities by collecting consecutive PlaceNames immediately before StateName.

    Returns:
        Tuple of (city, state) or (None, None) if extraction fails
    """
    if not address or not isinstance(address, str):
        return None, None

    try:
        parsed = usaddress.parse(address)
        state = None
        state_index = None
        city_parts = []

        for i, (value, component_type) in enumerate(parsed):
            if component_type == 'StateName':
                state = value.rstrip(',').strip().upper()
                state_index = i
                break

        if state is None or state_index is None:
            return None, None

        for i in range(state_index - 1, -1, -1):
            value, component_type = parsed[i]
            if component_type == 'PlaceName':
                city_parts.insert(0, value.rstrip(',').strip())
            else:
                break

        if not city_parts:
            return None, None

        city = ' '.join(city_parts)

        if state in _STATE_NAME_TO_ABBR:
            state = _STATE_NAME_TO_ABBR[state]

        if state == 'DC':
            city = 'Washington'

        return city, state

    except (usaddress.RepeatedLabelError, ValueError):
        return None, None
    except Exception:
        return None, None


def get_region_name(state):
    """Return display name for a US state abbreviation."""
    return _STATE_ABBR_TO_NAME.get(state.upper(), state)
