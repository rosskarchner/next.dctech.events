"""/updates — the weekly blog (replaces updates.dctech.events).

Named posts.py, not updates.py: calgen already has a top-level updates.py
module (the post-loading/summarizing layer this imports from) — a
same-named route module one level down would be confusing in imports and
greps even though it's technically a different path.
"""
import io
from datetime import datetime, time
from email.utils import formatdate
import xml.etree.ElementTree as ET  # nosec B405

import pytz
from flask import Response, render_template

from calgen.site_config import get_config
from calgen.updates import get_all_posts, get_free_post, get_update_post, summarize
from calgen.routes.common import local_tz, prepare_events_by_day


def register_routes(app):
    @app.route("/updates/")
    def updates_index():
        posts = get_all_posts()
        for post in posts:
            post['summary'] = summarize(post)
        cfg = get_config()
        return render_template('updates_index.html',
                               posts=posts,
                               base_url=cfg.get('base_url', ''))

    @app.route("/updates/<slug>/")
    def free_post(slug):
        post = get_free_post(slug)
        if not post:
            return "Update not found", 404
        cfg = get_config()
        return render_template('free_post.html',
                               post=post,
                               summary=summarize(post),
                               base_url=cfg.get('base_url', ''))

    @app.route("/updates/<int:year>/<int:month>/<int:day>/")
    def update_post(year, month, day):
        post = get_update_post(year, month, day)
        if not post:
            return "Update not found", 404
        days = prepare_events_by_day(post.get('events', []))
        cfg = get_config()
        return render_template('update_post.html',
                               post=post,
                               days=days,
                               summary=summarize(post),
                               base_url=cfg.get('base_url', ''))

    @app.route("/updates/feed.xml")
    def updates_rss_feed():
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        base_url = cfg.get('base_url', '')
        return _generate_updates_rss(get_all_posts(), site_name, base_url)


def _generate_updates_rss(posts, site_name, base_url):
    """RSS for the weekly posts.

    Deliberately separate from common.py's _generate_rss_feed: that one emits
    one item per *event* keyed by a content hash, while a blog feed needs one
    item per *post* with a stable permalink guid, so subscribers migrating
    from updates.dctech.events see one entry per week rather than per event.
    """
    rss = ET.Element('rss', version='2.0')
    rss.set('xmlns:atom', 'http://www.w3.org/2005/Atom')
    channel = ET.SubElement(rss, 'channel')
    ET.SubElement(channel, 'title').text = f"{site_name} Updates"
    ET.SubElement(channel, 'link').text = f"{base_url}/updates/"
    ET.SubElement(channel, 'description').text = (
        "Weekly roundups of technology events in and around Washington, DC"
    )
    ET.SubElement(channel, 'language').text = 'en-us'
    ET.SubElement(channel, 'lastBuildDate').text = formatdate(
        timeval=datetime.now(pytz.UTC).timestamp(), usegmt=True)
    atom_link = ET.SubElement(channel, 'atom:link')
    atom_link.set('href', f"{base_url}/updates/feed.xml")
    atom_link.set('rel', 'self')
    atom_link.set('type', 'application/rss+xml')

    for post in posts[:50]:
        item = ET.SubElement(channel, 'item')
        link = f"{base_url}{post['url']}"
        ET.SubElement(item, 'title').text = post['title']
        ET.SubElement(item, 'link').text = link

        if post.get('kind') == 'post':
            # Free-form posts carry their own rendered prose; the Markdown is
            # already HTML by this point, so ship it as the whole item body.
            body = [post.get('body_html', '')]
        elif post.get('kind') == 'link':
            # The item's own link is already the week page, so a "see the full
            # week" link underneath it would point at where the reader is
            # going anyway.
            body = [f"<p>{summarize(post)}</p>"]
        else:
            body = [f"<p>{summarize(post)}</p>"]
            # A roundup of what was *added* spans months, so pointing at one
            # week of the calendar would be a link to the wrong thing.
            if post.get('added_since'):
                body.append(
                    f'<p><a href="{base_url}/">'
                    f'See everything coming up on {site_name}</a></p>'
                )
            else:
                body.append(
                    f'<p><a href="{base_url}{post["week_url"]}">'
                    f'See the full week on {site_name}</a></p>'
                )
            listing = []
            for event in post.get('events', []):
                title = event.get('title')
                if not title:
                    continue
                event_url = event.get('url')
                label = f'<a href="{event_url}">{title}</a>' if event_url else title
                when = event.get('date', '')
                listing.append(f"<li>{when} — {label}</li>")
            if listing:
                body.append("<ul>" + ''.join(listing) + "</ul>")
        ET.SubElement(item, 'description').text = ''.join(body)

        guid = ET.SubElement(item, 'guid')
        guid.set('isPermaLink', 'true')
        guid.text = link

        # Using the post's own date keeps pubDate stable across rebuilds
        # instead of drifting to "now" on every site regeneration.
        pub_dt = local_tz.localize(
            datetime.combine(post['published_on'], time(hour=11)))
        ET.SubElement(item, 'pubDate').text = formatdate(
            timeval=pub_dt.astimezone(pytz.UTC).timestamp(), usegmt=True)

    tree = ET.ElementTree(rss)
    ET.indent(tree, space='  ')
    output = io.BytesIO()
    tree.write(output, encoding='utf-8', xml_declaration=True)
    return Response(output.getvalue(), mimetype='application/rss+xml')
