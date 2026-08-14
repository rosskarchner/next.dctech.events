#!/usr/bin/env python3
from flask_frozen import Freezer
import os

from calgen.app import (
    create_app, get_events, get_upcoming_weeks, get_categories, get_upcoming_months,
    get_category_month_combos, get_events_by_slug, get_all_week_ids, get_all_months,
    get_recently_added,
)
from calgen.updates import get_free_posts, get_update_posts
from calgen.site_config import get_config


def create_freezer(app):
    """Return a Freezer with all route generators registered."""
    plugin = app.region_plugin

    freezer = Freezer(app)

    @freezer.register_generator
    def month_page():
        # Archived months included: a month page that stops being generated
        # is deleted from the bucket by `s3 sync --delete` and 404s.
        for year, month in get_all_months():
            yield {'year': year, 'month': month}

    @freezer.register_generator
    def just_added_page():
        # Only when there is something to show: without the added_at export
        # the page would freeze as a permanent empty state.
        if get_recently_added():
            yield {}

    @freezer.register_generator
    def event_page():
        for slug in get_events_by_slug():
            yield {'slug': slug}

    @freezer.register_generator
    def event_ical():
        for slug in get_events_by_slug():
            yield {'slug': slug}

    @freezer.register_generator
    def region_page():
        if plugin:
            for region in plugin.list_regions():
                yield {'slug': region['slug']}

    @freezer.register_generator
    def week_page():
        for week_id in get_all_week_ids(12):
            yield {'week_id': week_id}

    @freezer.register_generator
    def locations_index():
        if plugin:
            yield {}

    @freezer.register_generator
    def approved_groups_list():
        yield {}

    @freezer.register_generator
    def virtual_events_page():
        yield {}

    @freezer.register_generator
    def category_page():
        for slug in get_categories().keys():
            yield {'slug': slug}

    @freezer.register_generator
    def category_month_page():
        for slug, year, month in get_category_month_combos():
            yield {'slug': slug, 'year': year, 'month': month}

    @freezer.register_generator
    def feeds_page():
        yield {}

    @freezer.register_generator
    def sitemap():
        yield {}

    @freezer.register_generator
    def events_json():
        yield {}

    @freezer.register_generator
    def ical_feed():
        yield {}

    @freezer.register_generator
    def events_rss_feed():
        yield {}

    @freezer.register_generator
    def category_ical_feed():
        for slug in get_categories().keys():
            yield {'slug': slug}

    @freezer.register_generator
    def location_ical_feed():
        if plugin:
            for region in plugin.list_regions():
                yield {'slug': region['slug']}

    @freezer.register_generator
    def category_rss_feed():
        for slug in get_categories().keys():
            yield {'slug': slug}

    @freezer.register_generator
    def location_rss_feed():
        if plugin:
            for region in plugin.list_regions():
                yield {'slug': region['slug']}

    @freezer.register_generator
    def updates_index():
        yield {}

    @freezer.register_generator
    def update_post():
        for post in get_update_posts():
            yield {'year': post['year'],
                   'month': post['month'],
                   'day': post['day']}

    @freezer.register_generator
    def free_post():
        for post in get_free_posts():
            yield {'slug': post['slug']}

    @freezer.register_generator
    def updates_rss_feed():
        yield {}

    @freezer.register_generator
    def not_found_page():
        yield {}

    return freezer


def main(site_dir=None, output_dir=None):
    if site_dir is None:
        site_dir = os.getcwd()
    site_dir = os.path.abspath(site_dir)

    if output_dir is None:
        output_dir = os.path.join(site_dir, 'build')

    app = create_app(site_dir)
    app.config['FREEZER_DESTINATION'] = output_dir
    app.config['FREEZER_RELATIVE_URLS'] = True

    os.makedirs(output_dir, exist_ok=True)

    freezer = create_freezer(app)
    freezer.freeze()

    import shutil
    categories_src = os.path.join(site_dir, 'static', 'categories.json')
    categories_dst = os.path.join(output_dir, 'static', 'categories.json')
    if os.path.exists(categories_src):
        os.makedirs(os.path.dirname(categories_dst), exist_ok=True)
        shutil.copy2(categories_src, categories_dst)

    print(f"Generated static site to {output_dir}")


if __name__ == '__main__':
    main()
