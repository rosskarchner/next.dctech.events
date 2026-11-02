"""/highlights/ — what a highlighted event is, Paid vs Editor's pick, the
price, and what is highlighted right now.

Buying happens on /edit/highlight.html, which asks the API for a live quote:
this page is frozen, so it is only as current as the last build.
"""
from flask import render_template

from calgen.routes.common import (UPCOMING_WINDOW_DAYS, get_events,
                                  select_highlights)
from calgen.site_config import get_config


def register_routes(app):
    @app.route('/highlights/')
    def highlights_page():
        price = str(get_config().get('highlight_price_usd', '') or '').strip()
        return render_template(
            'highlights.html',
            highlights=select_highlights(get_events()),
            window_days=UPCOMING_WINDOW_DAYS,
            price=price if price not in ('', '0') else None,
        )
