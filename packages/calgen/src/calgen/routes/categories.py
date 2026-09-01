"""Category pages, category x month facet pages, and category iCal/RSS/JSON."""
from datetime import date, timedelta
import calendar
import json

from flask import Response, render_template

from calgen.site_config import get_config
from calgen.routes.common import (
    THIN_FACET_MIN_EVENTS, get_events, get_categories, get_sidebar_data,
    filter_events_by_month, prepare_events_by_day, _generate_ical_feed,
    _generate_rss_feed,
)


def register_routes(app):
    @app.route("/categories/")
    def categories_index():
        categories = get_categories()
        events = get_events()
        categories_with_counts = []
        for slug, category in sorted(categories.items(), key=lambda x: x[1]['name']):
            count = len([e for e in events if slug in e.get('categories', [])])
            categories_with_counts.append({
                'slug': slug,
                'name': category['name'],
                'description': category.get('description', ''),
                'count': count
            })
        return render_template('categories_index.html', categories=categories_with_counts)

    @app.route("/categories/<slug>/")
    def category_page(slug):
        categories = get_categories()
        if slug not in categories:
            return "Category not found", 404
        events = get_events()
        filtered_events = [e for e in events if slug in e.get('categories', [])]
        days = prepare_events_by_day(filtered_events)
        category = categories[slug]
        stats = {'upcoming_events': len(filtered_events)}
        return render_template('category_page.html',
                               days=days,
                               stats=stats,
                               category_name=category['name'],
                               category_description=category.get('description', ''),
                               category_slug=slug,
                               sidebar=get_sidebar_data(active_category=slug))

    @app.route("/categories/<slug>/<int:year>/<int:month>/")
    def category_month_page(slug, year, month):
        """Show events for a single category within a single month."""
        categories = get_categories()
        if slug not in categories:
            return "Category not found", 404
        try:
            if month < 1 or month > 12:
                return "Invalid month", 404
            first_day = date(year, month, 1)
            if month == 12:
                last_day = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                last_day = date(year, month + 1, 1) - timedelta(days=1)
        except ValueError:
            return "Invalid date", 404

        events = get_events()
        category_events = [e for e in events if slug in e.get('categories', [])]
        month_events = filter_events_by_month(category_events, first_day, last_day)
        days = prepare_events_by_day(month_events)
        category = categories[slug]
        stats = {'upcoming_events': len(month_events)}
        return render_template('category_month_page.html',
                               days=days,
                               stats=stats,
                               category_name=category['name'],
                               category_description=category.get('description', ''),
                               category_slug=slug,
                               month_name=calendar.month_name[month],
                               year=year,
                               month=month,
                               is_thin=len(month_events) < THIN_FACET_MIN_EVENTS,
                               sidebar=get_sidebar_data(active_category=slug,
                                                        active_month=(year, month)))

    @app.route("/categories.json")
    def categories_json():
        categories = get_categories()
        formatted = {
            slug: {'slug': slug, 'name': cat.get('name', slug), 'description': cat.get('description', '')}
            for slug, cat in categories.items()
        }
        return Response(json.dumps(formatted, indent=2), mimetype='application/json')

    @app.route("/categories/<slug>/feed.ics")
    def category_ical_feed(slug):
        categories = get_categories()
        if slug not in categories:
            return "Category not found", 404
        events = get_events()
        filtered_events = [e for e in events if slug in e.get('categories', [])]
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        category = categories[slug]
        return _generate_ical_feed(
            filtered_events,
            f"{category['name']} Events - {site_name}",
            f"{category['name']} events"
        )

    @app.route("/categories/<slug>/feed.xml")
    def category_rss_feed(slug):
        categories = get_categories()
        if slug not in categories:
            return "Category not found", 404
        events = get_events()
        filtered_events = [e for e in events if slug in e.get('categories', [])]
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        base_url = cfg.get('base_url', '')
        category = categories[slug]
        return _generate_rss_feed(
            filtered_events,
            f"{category['name']} Events - {site_name}",
            f"Upcoming {category['name']} events",
            f"{base_url}/categories/{slug}/"
        )
