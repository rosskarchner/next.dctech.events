"""
One-time site setup helpers: config.yaml, static/robots.txt, regions.py.

Shared by `calgen bootstrap` (CLI) and the MCP server's bootstrap_site /
write_regions tools, so both produce identical output.
"""
import os
import pprint

import yaml

_CONFIG_FILE = 'config.yaml'


def write_config(site_dir, *, city, domain, timezone, table_name,
                  max_distance_miles=40, tagline=None,
                  add_events_link=None, newsletter_signup_link=None) -> str:
    """Write config.yaml for a new site. Returns the path written."""
    config = {
        'site_name': f"{city} Tech Events",
        'tagline': tagline or f"Technology conferences and meetups in and around {city}",
        'base_url': f"https://{domain}",
        'add_events_link': add_events_link or f"https://{domain}/edit/submit-event.html",
        'newsletter_signup_link': newsletter_signup_link or f"https://newsletter.{domain}/",
        'timezone': timezone,
        'location': city,
        'max_distance_miles': max_distance_miles,
        'github_client_id': '',
        'oauth_callback_endpoint': f"https://add.{domain}/oauth/callback",
        'github_sponsors_token': '',
        'dynamodb_table': table_name,
    }
    path = os.path.join(site_dir, _CONFIG_FILE)
    with open(path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return path


def write_robots(site_dir, *, domain) -> str:
    """Write static/robots.txt for a new site. Returns the path written."""
    path = os.path.join(site_dir, 'static', 'robots.txt')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: https://{domain}/sitemap.xml\n"
    )
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path


_REGIONS_TEMPLATE = '''from calgen.location_utils import extract_location_info
from calgen.regions import EventRejected

# {city} metro regions
_REGIONS = {regions}

# The home state for this site. Anything outside this state is rejected.
_HOME_STATE = {home_state!r}

# Known suburb/city names mapped to their region slug.
# Keys are lowercased city names as returned by extract_location_info().
_CITY_TO_REGION = {city_to_region}

# Default region for an in-state location we don't otherwise recognize.
_DEFAULT_SLUG = {default_slug!r}


def list_regions():
    return _REGIONS


def location_to_region(location_str):
    if not location_str:
        return None

    city_name, state = extract_location_info(location_str)

    if not state:
        return None

    state_upper = state.upper()
    if state_upper != _HOME_STATE:
        raise EventRejected(
            "Event is in " + state_upper + ", outside the {city} metro area"
        )

    if not city_name:
        return _DEFAULT_SLUG

    key = city_name.strip().lower()
    return _CITY_TO_REGION.get(key, _DEFAULT_SLUG)
'''


def write_regions_file(site_dir, *, city, home_state, default_slug, regions, city_to_region) -> str:
    """
    Write regions.py — the metro-area region plugin.

    `regions` is a list of {'slug': ..., 'name': ...} dicts. `city_to_region`
    maps lowercased city/suburb names to a region slug. `default_slug` must be
    one of the slugs in `regions` and is used for in-state locations not found
    in `city_to_region`.
    """
    slugs = {r['slug'] for r in regions}
    if default_slug not in slugs:
        raise ValueError(f"default_slug {default_slug!r} is not among region slugs {sorted(slugs)}")
    unknown = set(city_to_region.values()) - slugs
    if unknown:
        raise ValueError(f"city_to_region maps to unknown region slug(s): {sorted(unknown)}")

    content = _REGIONS_TEMPLATE.format(
        city=city,
        home_state=home_state.upper(),
        default_slug=default_slug,
        regions=pprint.pformat(regions, width=88, sort_dicts=False),
        city_to_region=pprint.pformat(city_to_region, width=88, sort_dicts=False),
    )
    path = os.path.join(site_dir, 'regions.py')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path
