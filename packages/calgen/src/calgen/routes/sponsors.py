"""/sponsor/ — what a weekly sponsorship is, what it costs, and who has run.

Booking itself happens on /edit/sponsor.html, which asks the API for live
availability: this page is frozen, so it would be wrong about open weeks for
up to a day, and it never shows holds or unreviewed bookings at all.
"""
from datetime import datetime

from flask import render_template

from calgen.routes.common import get_week_identifier, local_tz
from calgen.site_config import get_config


def register_routes(app):
    @app.route('/sponsor/')
    def sponsor_page():
        from calgen.app import _load_sponsors
        sponsors = _load_sponsors()
        this_week = get_week_identifier(datetime.now(local_tz).date())
        upcoming = [s for s in sponsors if s.get('week_id', '') >= this_week]
        past = [s for s in sponsors if s.get('week_id', '') < this_week]
        price = str(get_config().get('sponsorship_price_usd', '') or '').strip()
        return render_template(
            'sponsor.html',
            upcoming_sponsors=upcoming,
            past_sponsors=list(reversed(past)),
            this_week=this_week,
            price=price if price not in ('', '0') else None,
        )
