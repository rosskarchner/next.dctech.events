"""The /updates blog — three kinds of post, one timeline.

*Weekly roundups* (`kind: 'weekly'`) are snapshots, not derived views.
`get_events()` and the pipeline both drop anything older than today, so a
roundup rendered from live event data would quietly empty out as the events in
it happened. A publisher instead writes one YAML file per week into
`_updates/` at publish time, freezing that week's listing.

What a roundup lists changed on 2026-08-12: posts from then on carry a
`published_on` date and list the events *added* that week, while older ones are
filed under `week_start` and list the events *happening* that week. Nothing
here has to know which is which — a snapshot carries its own title and its own
`summary`, so both render as themselves.

*Link posts* (`kind: 'link'`) are the Monday "week ahead" post: a title, a
blurb, and a `link_url` pointing at /week/<week_id>/. They live in `_updates/`
alongside roundups and are told apart by a `post_kind: link` key.

Two things make them unlike the others. They carry no events — they do not have
to freeze a listing, because the page they point at merges live events with its
own `_archive/` capture, so the link keeps working after the week is over. And
they build no page: a link post's `url` *is* its target, so it appears on
/updates/ and in the feed but sends the reader straight to the week. See
`get_paged_update_posts`.

*Free-form posts* (`kind: 'post'`) are hand-written announcements authored in
/edit, exported to `_posts/` as YAML with a Markdown body. They carry their own
slug and date rather than being tied to a week.

All three are pure reads of on-disk files — this module never invents a post —
and `get_all_posts()` interleaves them by date for the index and the feed.
"""
import os
from datetime import date, datetime

import markdown
import yaml
from bs4 import BeautifulSoup

UPDATES_DIR = '_updates'
POSTS_DIR = '_posts'


def _parse_date(value):
    """Accept a real date (PyYAML parses bare YYYY-MM-DD) or a string."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            return None
    return None


def _load_post(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None

    # The date the post is filed under: its own publication date if it has
    # one, else the Monday of the week it covers, which is how roundups were
    # dated before they became "what was added this week".
    week_start = _parse_date(data.get('week_start'))
    filed_on = _parse_date(data.get('published_on')) or week_start
    if not filed_on or not data.get('week_id'):
        return None

    week_end = _parse_date(data.get('week_end'))
    events = data.get('events') or []

    post = dict(data)
    post['week_start'] = week_start or filed_on
    post['week_end'] = week_end
    post['events'] = events
    post['event_count'] = data.get('event_count', len(events))
    post['year'] = filed_on.year
    post['month'] = filed_on.month
    post['day'] = filed_on.day
    # Unpadded to match Flask's <int:> converter (and _month_url's existing
    # /{year}/{month}/ convention) — a padded literal here would not match the
    # path Frozen-Flask actually writes.
    post['week_url'] = f"/week/{data['week_id']}/"
    post['week_start_formatted'] = filed_on.strftime('%B %-d, %Y')
    if not post.get('title'):
        post['title'] = (
            f"DC Tech Events for the week of {post['week_start_formatted']}"
        )
    # A link post points somewhere instead of listing anything; the publisher
    # says which by writing `post_kind`, and only ever writes it for those.
    post['kind'] = 'link' if data.get('post_kind') == 'link' else 'weekly'
    # Stored by the publisher so the target can be changed without a deploy,
    # but defaulted here so a link post is never left with a dead link.
    post['link_url'] = str(data.get('link_url') or '').strip() or post['week_url']
    # A link post's `url` is its target: it builds no page under /updates/, so
    # the index, the feed and the sitemap all send readers straight to the week
    # it is announcing. Every other kind links the page it owns.
    post['url'] = (
        post['link_url'] if post['kind'] == 'link'
        else f"/updates/{filed_on.year}/{filed_on.month}/{filed_on.day}/"
    )
    post['published_on'] = filed_on
    post['date_formatted'] = post['week_start_formatted']
    return post


def _load_free_post(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None

    slug = str(data.get('slug') or '').strip()
    published_on = _parse_date(data.get('published_on'))
    if not slug or not published_on:
        return None

    # Drafts are exported alongside published posts so /edit can list them;
    # the build is where they get filtered out, so a draft can never reach
    # the index, the feed, or a URL of its own.
    if str(data.get('status', 'draft')).lower() != 'published':
        return None

    post = dict(data)
    post['slug'] = slug
    post['published_on'] = published_on
    post['kind'] = 'post'
    post['url'] = f"/updates/{slug}/"
    post['date_formatted'] = published_on.strftime('%B %-d, %Y')
    post['title'] = str(data.get('title') or slug).strip()
    post['body_html'] = markdown.markdown(
        str(data.get('body') or ''),
        extensions=['extra', 'sane_lists'],
    )
    return post


def get_free_posts():
    """Published free-form posts, newest first."""
    if not os.path.isdir(POSTS_DIR):
        return []

    posts = []
    for filename in sorted(os.listdir(POSTS_DIR)):
        if not filename.endswith(('.yaml', '.yml')):
            continue
        post = _load_free_post(os.path.join(POSTS_DIR, filename))
        if post:
            posts.append(post)

    posts.sort(key=lambda p: (p['published_on'], p['slug']), reverse=True)
    return posts


def get_free_post(slug):
    """A single published free-form post by slug, or None."""
    for post in get_free_posts():
        if post['slug'] == slug:
            return post
    return None


def get_all_posts():
    """Every kind of post interleaved by date, newest first."""
    posts = get_update_posts() + get_free_posts()
    # Slug is a stable tiebreaker so a free-form post published on a Monday
    # does not reorder between builds relative to that week's roundup.
    posts.sort(key=lambda p: (p['published_on'], p.get('slug', '')),
               reverse=True)
    return posts


def get_update_posts():
    """All published posts, newest first."""
    if not os.path.isdir(UPDATES_DIR):
        return []

    posts = []
    for filename in sorted(os.listdir(UPDATES_DIR)):
        if not filename.endswith(('.yaml', '.yml')):
            continue
        post = _load_post(os.path.join(UPDATES_DIR, filename))
        if post:
            posts.append(post)

    posts.sort(key=lambda p: p['published_on'], reverse=True)
    return posts


def get_paged_update_posts():
    """The posts that own a page under /updates/.

    Link posts do not: they are index-and-feed entries whose link goes
    straight to /week/. Both the Frozen-Flask generator and the route below
    read this rather than get_update_posts(), so a link post can never build a
    page and its /updates/ path stays a 404.
    """
    return [post for post in get_update_posts() if post['kind'] != 'link']


def get_update_post(year, month, day):
    """The post filed on the given date that owns a page, or None."""
    for post in get_paged_update_posts():
        if (post['year'], post['month'], post['day']) == (year, month, day):
            return post
    return None


def _summarize_free_post(post, max_chars=200):
    """An explicit summary if the author wrote one, else the opening prose.

    Derived from the *rendered* HTML rather than the Markdown source, so the
    blurb reads as prose: no stray ``**bold**`` or ``[link](url)`` syntax, and
    no truncation at an arbitrary source line break.
    """
    explicit = str(post.get('summary') or '').strip()
    if explicit:
        return explicit

    html = post.get('body_html') or ''
    if not html:
        return ''

    soup = BeautifulSoup(html, 'html.parser')
    # First paragraph, so a post opening with a heading blurbs its prose.
    paragraph = soup.find('p')
    # No separator: inline elements have to butt up against the punctuation
    # that follows them ("see the groups." not "see the groups ."). Collapsing
    # whitespace afterwards still rejoins a paragraph wrapped across lines.
    text = ' '.join((paragraph or soup).get_text().split())
    if not text:
        return ''
    if len(text) > max_chars:
        # Prefer a word boundary over slicing mid-word.
        clipped = text[:max_chars].rsplit(' ', 1)[0].rstrip(' ,;:—-')
        return (clipped or text[:max_chars].rstrip()) + '…'
    return text


def summarize(post, max_titles=3):
    """Short human blurb used in the index and in feed descriptions."""
    if post.get('kind') == 'post':
        return _summarize_free_post(post)

    # A link post stores no events, so there is nothing to derive a blurb
    # from; the publisher always writes one.
    if post.get('kind') == 'link':
        return str(post.get('summary') or '').strip()

    # A roundup written from 2026-08-12 on states its own span ("12 events
    # added between August 5–11"), which nothing here could reconstruct: the
    # snapshot records what was added, not when the week was.
    explicit = str(post.get('summary') or '').strip()
    if explicit:
        return explicit

    titles = [e.get('title') for e in post.get('events', []) if e.get('title')]
    count = post.get('event_count', len(titles))
    if not titles:
        return "No events were listed for this week."

    shown = titles[:max_titles]
    remaining = count - len(shown)
    blurb = ', '.join(shown)
    if remaining > 0:
        blurb += f", and {remaining} more"
    return f"{count} event{'s' if count != 1 else ''} this week: {blurb}."
