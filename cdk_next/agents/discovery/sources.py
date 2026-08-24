"""Sites scanned on every discovery run."""

SOURCES = [
    {
        'url': 'https://technical.ly/dc/events/',
        'kind': 'listing',
        'note': 'DC tech news site; strong on startup and civic-tech events',
    },
    {
        'url': 'https://www.eventbrite.com/d/dc--washington/technology/',
        'kind': 'listing',
        'note': 'Heavy JS — browser tool only. High noise: filter hard.',
    },
    {
        'url': 'https://www.meetup.com/find/?keywords=technology&location=us--dc--Washington',
        'kind': 'listing',
        'note': 'Prefer proposing the GROUP, not the individual event.',
    },
]
