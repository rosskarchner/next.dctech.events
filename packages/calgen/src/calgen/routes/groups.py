"""Organizer group listing — the smallest resource area, one route.

First route module split out of app.py (next_dctech_events-5m0), chosen as
the proof that the register_routes(app) pattern itself works (no shared
closures, no other module depends on it) before repeating it for the rest.
"""
from flask import render_template

from calgen.routes.common import get_approved_groups


def register_routes(app):
    @app.route("/groups/")
    def approved_groups_list():
        groups = get_approved_groups()
        return render_template('approved_groups_list.html',
                               groups=groups,
                               next_key=None,
                               has_next=False)
