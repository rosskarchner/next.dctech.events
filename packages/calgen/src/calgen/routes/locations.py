"""Location/region pages and their per-region iCal/RSS feeds."""
from flask import render_template

from calgen.site_config import get_config
from calgen.routes.common import (
    get_events, prepare_events_by_day, _generate_ical_feed, _generate_rss_feed,
)


def register_routes(app):
    @app.route("/locations/")
    def locations_index():
        plugin = app.region_plugin
        if not plugin:
            return "Not found", 404
        events = get_events()
        counts = {}
        for event in events:
            slug = event.get('region')
            if slug:
                counts[slug] = counts.get(slug, 0) + 1
        locations = [
            {'state': r['slug'], 'name': r['name'], 'count': counts.get(r['slug'], 0)}
            for r in plugin.list_regions()
            if counts.get(r['slug'], 0) > 0
        ]
        return render_template('locations_index.html', locations=locations)

    @app.route("/locations/<slug>/")
    def region_page(slug):
        plugin = app.region_plugin
        if not plugin:
            return "Region not found", 404
        regions = plugin.list_regions()
        region = next((r for r in regions if r['slug'] == slug), None)
        if not region:
            return "Region not found", 404
        events = get_events()
        filtered_events = [e for e in events if e.get('region') == slug]
        days = prepare_events_by_day(filtered_events)
        stats = {'upcoming_events': len(filtered_events)}
        return render_template('location_page.html',
                               days=days,
                               stats=stats,
                               location_name=region['name'],
                               location_type='region')

    @app.route("/locations/<slug>/feed.ics")
    def location_ical_feed(slug):
        plugin = app.region_plugin
        if not plugin:
            return "Region not found", 404
        regions = plugin.list_regions()
        region = next((r for r in regions if r['slug'] == slug), None)
        if not region:
            return "Region not found", 404
        events = get_events()
        filtered_events = [e for e in events if e.get('region') == slug]
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        return _generate_ical_feed(
            filtered_events,
            f"{region['name']} Events - {site_name}",
            f"Technology events in {region['name']}"
        )

    @app.route("/locations/<slug>/feed.xml")
    def location_rss_feed(slug):
        plugin = app.region_plugin
        if not plugin:
            return "Region not found", 404
        regions = plugin.list_regions()
        region = next((r for r in regions if r['slug'] == slug), None)
        if not region:
            return "Region not found", 404
        events = get_events()
        filtered_events = [e for e in events if e.get('region') == slug]
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        base_url = cfg.get('base_url', '')
        return _generate_rss_feed(
            filtered_events,
            f"{region['name']} Events - {site_name}",
            f"Upcoming technology events in {region['name']}",
            f"{base_url}/locations/{slug}/"
        )
