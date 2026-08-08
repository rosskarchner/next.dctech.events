"""The /updates blog — two kinds of post, one timeline.

*Weekly roundups* are snapshots, not derived views. `get_events()` and the
pipeline both drop anything older than today, so a roundup rendered from live
event data would quietly empty out as its week receded into the past. A
publisher instead writes one YAML file per ISO week into `_updates/` at publish
time, freezing that week's listing.

*Free-form posts* are hand-written announcements authored in /edit, exported to
`_posts/` as YAML with a Markdown body. They carry their own slug and date
rather than being tied to a week.

Both are pure reads of on-disk files — this module never invents a post — and
`get_all_posts()` interleaves them by date for the index and the feed.
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

    week_start = _parse_date(data.get('week_start'))
    if not week_start or not data.get('week_id'):
        return None

    week_end = _parse_date(data.get('week_end'))
    events = data.get('events') or []

    post = dict(data)
    post['week_start'] = week_start
    post['week_end'] = week_end
    post['events'] = events
    post['event_count'] = data.get('event_count', len(events))
    post['year'] = week_start.year
    post['month'] = week_start.month
    post['day'] = week_start.day
    # Unpadded to match Flask's <int:> converter (and _month_url's existing
    # /{year}/{month}/ convention) — a padded literal here would not match the
    # path Frozen-Flask actually writes.
    post['url'] = f"/updates/{week_start.year}/{week_start.month}/{week_start.day}/"
    post['week_url'] = f"/week/{data['week_id']}/"
    post['week_start_formatted'] = week_start.strftime('%B %-d, %Y')
    if not post.get('title'):
        post['title'] = (
            f"DC Tech Events for the week of {post['week_start_formatted']}"
        )
    post['kind'] = 'weekly'
    post['published_on'] = week_start
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
    """Weekly roundups and free-form posts interleaved, newest first."""
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

    posts.sort(key=lambda p: p['week_start'], reverse=True)
    return posts


def get_update_post(year, month, day):
    """The post whose week starts on the given date, or None."""
    for post in get_update_posts():
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
