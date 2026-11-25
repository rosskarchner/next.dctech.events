"""Site-wide feeds index, sitemap, and site-wide events JSON/iCal/RSS."""
import json
import os
from datetime import timedelta

from flask import Response, render_template

from calgen.lastmod import get_event_lastmods
from calgen.location_utils import get_region_name
from calgen.site_config import get_config
from calgen.structured import iana_timezone
from calgen.updates import get_all_posts
from calgen.routes.common import (
    THIN_FACET_MIN_EVENTS, get_events, get_categories, get_events_by_slug,
    get_recently_added, get_all_months, get_all_week_ids,
    get_category_month_counts, get_week_identifier, local_today, _generate_ical_feed,
    _generate_rss_feed, public_events_json,
)


def register_routes(app):
    @app.route("/feeds/")
    def feeds_page():
        cfg = get_config()
        only_states = cfg.get('only_states', [])
        categories = get_categories()
        events = get_events()

        categories_with_counts = []
        for slug, category in sorted(categories.items(), key=lambda x: x[1]['name']):
            count = len([e for e in events if slug in e.get('categories', [])])
            if count > 0:
                categories_with_counts.append({'slug': slug, 'name': category['name'], 'count': count})

        locations = [
            {'state': s.lower(), 'name': get_region_name(s.upper())}
            for s in only_states
        ]

        return render_template('feeds.html',
                               categories=categories_with_counts,
                               locations=locations)

    @app.route("/sitemap.xml")
    def sitemap():
        """Every indexable frozen URL, and an honest lastmod or none at all.

        Two deliberate omissions. `changefreq` is gone entirely — Google has
        not used it for years, and it was never a request anyway. `lastmod` is
        emitted only for the /updates posts that own a page, where
        `published_on` is a real content date; every listing page is derived
        from the current event set and has no per-page change date we can
        compute. Stamping build time on
        all of them, as this function used to, is worse than saying nothing:
        it makes every URL look freshly changed on every rebuild, and crawlers
        respond by discounting the field sitewide.

        Thin category+month facets are excluded — they carry noindex (see
        THIN_FACET_MIN_EVENTS), so listing them would only send crawlers to
        pages we have asked them to drop.
        """
        cfg = get_config()
        base_url = cfg.get('base_url', '')

        urls = [
            {'loc': f"{base_url}/"},
            {'loc': f"{base_url}/virtual/"},
            {'loc': f"{base_url}/categories/"},
            {'loc': f"{base_url}/groups/"},
            {'loc': f"{base_url}/feeds/"},
        ]

        plugin = app.region_plugin
        if plugin:
            urls.append({'loc': f"{base_url}/locations/"})
            for region in plugin.list_regions():
                urls.append({'loc': f"{base_url}/locations/{region['slug']}/"})

        for slug in get_categories().keys():
            urls.append({'loc': f"{base_url}/categories/{slug}/"})

        # Event pages carry the date `calgen lastmod` last saw their rendered
        # content change, carried across builds; absent when unknown. Unlike
        # the listing pages, a single event has a real change date to report.
        event_lastmods = get_event_lastmods()
        for slug in get_events_by_slug():
            url = {'loc': f"{base_url}/events/{slug}/"}
            if event_lastmods.get(slug):
                url['lastmod'] = event_lastmods[slug]
            urls.append(url)

        if get_recently_added():
            urls.append({'loc': f"{base_url}/just-added/"})

        for year, month in get_all_months():
            urls.append({'loc': f"{base_url}/{year}/{month}/"})

        for week_id in get_all_week_ids(12):
            urls.append({'loc': f"{base_url}/week/{week_id}/"})

        for (slug, year, month), count in sorted(get_category_month_counts().items()):
            if count >= THIN_FACET_MIN_EVENTS:
                urls.append({'loc': f"{base_url}/categories/{slug}/{year}/{month}/"})

        urls.append({'loc': f"{base_url}/updates/"})
        for post in get_all_posts():
            # A link post's url is the /week/ page it points at, already
            # listed above — emitting it again would duplicate a <loc>.
            if post.get('kind') == 'link':
                continue
            urls.append({'loc': f"{base_url}{post['url']}",
                         'lastmod': post['published_on'].strftime('%Y-%m-%d')})

        xml = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for url in urls:
            xml.append('  <url>')
            xml.append(f'    <loc>{url["loc"]}</loc>')
            if url.get('lastmod'):
                xml.append(f'    <lastmod>{url["lastmod"]}</lastmod>')
            xml.append('  </url>')
        xml.append('</urlset>')
        return Response('\n'.join(xml), mimetype='application/xml')

    @app.route("/llms.txt")
    def llms_txt():
        """A map of the site for language-model agents (llmstxt.org).

        Says what the site covers, where the machine-readable data is, and
        the one rule that matters: details belong to the organizer, so link
        there and tell people to confirm. Rendered per build so the category
        and location lists stay current.
        """
        cfg = get_config()
        plugin = app.region_plugin
        events = get_events()
        categories = []
        for slug, category in sorted(get_categories().items(), key=lambda x: x[1]['name']):
            count = sum(1 for e in events if slug in e.get('categories', []))
            if count:
                categories.append({'slug': slug, 'name': category['name'],
                                   'description': category.get('description', ''),
                                   'count': count})
        regions = []
        if plugin:
            for region in plugin.list_regions():
                count = sum(1 for e in events if e.get('region') == region['slug'])
                if count:
                    regions.append({'slug': region['slug'], 'name': region['name'],
                                    'count': count})
        body = render_template('llms.txt',
                               base_url=cfg.get('base_url', ''),
                               timezone=iana_timezone(),
                               total=len(events),
                               categories=categories,
                               regions=regions,
                               week_ids=[get_week_identifier(local_today() + timedelta(weeks=i))
                                         for i in range(4)])
        return Response(body, mimetype='text/plain')

    @app.route("/calendar.json")
    def calendar_json():
        """Every upcoming event in the public shape (calgen.structured).

        /events.json stays as it is: its internal record shape is what the
        MCP tools and existing scripts read, guid included.
        """
        return public_events_json(get_events(), title="Every upcoming event", page="/")

    @app.route("/events.json")
    def events_json():
        events_json_file = os.path.join('_data', 'events.json')
        if os.path.exists(events_json_file):
            with open(events_json_file, 'r', encoding='utf-8') as f:
                return Response(f.read(), mimetype='application/json')
        events = get_events()
        return Response(json.dumps(events, indent=2), mimetype='application/json')

    @app.route("/events.ics")
    def ical_feed():
        events = get_events()
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        tagline = cfg.get('tagline', '')
        return _generate_ical_feed(events, site_name, tagline)

    @app.route("/events-feed.xml")
    def events_rss_feed():
        events = get_events()
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        base_url = cfg.get('base_url', '')
        return _generate_rss_feed(events, f"{site_name}", f"Upcoming events", f"{base_url}/")
