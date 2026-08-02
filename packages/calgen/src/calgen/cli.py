#!/usr/bin/env python3
"""
calgen — static site generator for local tech event calendars.

Commands:
  calgen init      Create a new site directory
  calgen serve     Run the Flask development server
  calgen refresh   Fetch iCal feeds and update the event cache
  calgen pipeline  Generate _data/all_events.json from cached feeds
  calgen build     Freeze the site to static HTML
  calgen rebuild   refresh + pipeline + build in one shot
"""
import os
import sys
import click


@click.group()
def cli():
    """calgen: static site generator for local tech event calendars."""
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prepare_site_dir(site_dir):
    """Resolve site_dir, chdir, set env var, and clear the config cache."""
    site_dir = os.path.abspath(site_dir)
    if not os.path.isdir(site_dir):
        click.echo(f"Error: site directory not found: {site_dir}", err=True)
        sys.exit(1)
    os.environ['CALGEN_SITE_DIR'] = site_dir
    os.chdir(site_dir)
    from calgen.site_config import reset_config
    reset_config()
    return site_dir


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@cli.command()
@click.argument('site_dir', default='.', metavar='[DIRECTORY]')
def init(site_dir):
    """Create a new calgen site in DIRECTORY (default: current directory).

    Prompts for site metadata and writes config.yaml plus the standard
    directory layout.  Safe to re-run — existing files are not overwritten.
    """
    site_dir = os.path.abspath(site_dir)
    os.makedirs(site_dir, exist_ok=True)

    config_path = os.path.join(site_dir, 'config.yaml')
    if os.path.exists(config_path):
        click.echo(f"config.yaml already exists in {site_dir} — skipping config generation.")
        click.echo("Delete config.yaml and re-run to regenerate it.")
        _create_directories(site_dir)
        return

    click.echo(f"\nInitializing new calgen site in: {site_dir}\n")

    site_name = click.prompt("Site name", default="My Tech Events")
    tagline = click.prompt("Tagline", default="Technology conferences and meetups")
    base_url = click.prompt("Base URL (no trailing slash)", default="https://example.com")
    timezone = click.prompt("Timezone", default="US/Eastern")

    click.echo("\nOptional: restrict events to specific US states (e.g. DC, MD, VA).")
    states_input = click.prompt(
        "States to include (comma-separated, leave blank for all)", default=""
    )
    only_states = [s.strip().upper() for s in states_input.split(',') if s.strip()]

    add_events_link = click.prompt("Add-event link (leave blank to skip)", default="")
    newsletter_link = click.prompt("Newsletter signup link (leave blank to skip)", default="")

    _write_config(
        config_path,
        site_name=site_name,
        tagline=tagline,
        base_url=base_url,
        timezone=timezone,
        only_states=only_states,
        add_events_link=add_events_link,
        newsletter_signup_link=newsletter_link,
    )

    _create_directories(site_dir)
    _write_gitignore(site_dir)
    _write_example_group(site_dir)

    click.echo(f"\nSite initialized in {site_dir}")
    click.echo("\nNext steps:")
    click.echo(f"  cd {site_dir}")
    click.echo("  Add group iCal feeds to _groups/*.yaml")
    click.echo("  calgen refresh    # fetch calendar data")
    click.echo("  calgen pipeline   # generate event list")
    click.echo("  calgen serve      # preview locally")
    click.echo("  calgen build      # freeze to static HTML")


def _write_config(path, *, site_name, tagline, base_url, timezone, only_states,
                  add_events_link, newsletter_signup_link):
    import yaml
    config = {
        'site_name': site_name,
        'tagline': tagline,
        'base_url': base_url,
        'timezone': timezone,
    }
    if only_states:
        config['only_states'] = only_states
    if add_events_link:
        config['add_events_link'] = add_events_link
    if newsletter_signup_link:
        config['newsletter_signup_link'] = newsletter_signup_link

    with open(path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    click.echo(f"  Created {path}")


def _create_directories(site_dir):
    dirs = [
        '_groups',
        '_categories',
        '_single_events',
        '_recurring_events',
        '_data',
        '_cache/ical',
        '_cache/json-ld',
        'static/css',
        'templates',
    ]
    for d in dirs:
        full = os.path.join(site_dir, d)
        os.makedirs(full, exist_ok=True)
    click.echo(f"  Created standard directory layout in {site_dir}")


def _write_gitignore(site_dir):
    path = os.path.join(site_dir, '.gitignore')
    if os.path.exists(path):
        return
    content = (
        "# calgen build artifacts\n"
        "_cache/\n"
        "_data/\n"
        "build/\n"
        "*.pyc\n"
        "__pycache__/\n"
        ".venv/\n"
    )
    with open(path, 'w') as f:
        f.write(content)
    click.echo(f"  Created {path}")


def _write_example_group(site_dir):
    path = os.path.join(site_dir, '_groups', 'example-group.yaml')
    if os.path.exists(path):
        return
    content = (
        "# Example group file — rename this file and fill in the fields.\n"
        "# The filename (without .yaml) is the group's slug.\n"
        "name: Example Group\n"
        "website: https://example.com/group\n"
        "ical: https://www.meetup.com/your-group/events/ical/\n"
        "active: true\n"
        "# categories: [python, javascript]  # optional list of category slugs\n"
    )
    with open(path, 'w') as f:
        f.write(content)
    click.echo(f"  Created {path}")


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------

@cli.command()
@click.argument('site_dir', default='.', metavar='[DIRECTORY]')
@click.option('--city', required=True, help="e.g. 'Richmond, VA'")
@click.option('--domain', required=True, help="Site hostname, e.g. richmond.localtech.events")
@click.option('--timezone', required=True, help="IANA tz name, e.g. US/Eastern")
@click.option('--table-name', default=None, help="DynamoDB table name (default: derived from city)")
@click.option('--max-distance-miles', default=40, show_default=True)
@click.option('--home-state', default=None, help="Two-letter state code for regions.py (default: derived from city)")
@click.option('--default-region-slug', default=None, help="Default region slug for regions.py (default: derived from city)")
@click.option('--default-region-name', default=None, help="Default region display name (default: city name)")
def bootstrap(site_dir, city, domain, timezone, table_name, max_distance_miles,
               home_state, default_region_slug, default_region_name):
    """Write config.yaml, static/robots.txt, and a starter regions.py for a new site.

    This covers the one-time setup files that don't have a dedicated MCP tool
    for their full content (regions.py needs metro-area research). Run this
    first, then refine regions.py with additional regions/suburb mappings as
    needed — or use the `write_regions` MCP tool to write a complete version.
    """
    site_dir = _prepare_site_dir(site_dir)
    from calgen import bootstrap as bootstrap_mod

    city_name = city.split(',')[0].strip()
    if not table_name:
        table_name = ''.join(w.capitalize() for w in city_name.split()) + 'TechEvents'
    if not home_state:
        home_state = city.split(',')[-1].strip().upper() if ',' in city else 'XX'
    if not default_region_slug:
        import re
        default_region_slug = re.sub(r'[^a-z0-9]+', '-', city_name.lower()).strip('-')
    if not default_region_name:
        default_region_name = city_name

    config_path = bootstrap_mod.write_config(
        site_dir, city=city, domain=domain, timezone=timezone,
        table_name=table_name, max_distance_miles=max_distance_miles,
    )
    click.echo(f"  Wrote {config_path}")

    robots_path = bootstrap_mod.write_robots(site_dir, domain=domain)
    click.echo(f"  Wrote {robots_path}")

    regions_path = bootstrap_mod.write_regions_file(
        site_dir, city=city, home_state=home_state, default_slug=default_region_slug,
        regions=[{'slug': default_region_slug, 'name': default_region_name}],
        city_to_region={},
    )
    click.echo(f"  Wrote {regions_path} (starter — add more regions/suburb mappings as needed)")


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

@cli.command()
@click.option('--site-dir', default='.', type=click.Path(), help='Site directory (default: .)')
@click.option('--port', default=5000, show_default=True, help='Port to serve on')
@click.option('--host', default='127.0.0.1', show_default=True, help='Host to bind to')
def serve(site_dir, port, host):
    """Run the Flask development server."""
    site_dir = _prepare_site_dir(site_dir)
    from calgen.app import create_app
    app = create_app(site_dir)
    click.echo(f"Serving site at http://{host}:{port}/  (site dir: {site_dir})")
    app.run(host=host, port=port, debug=True)


# ---------------------------------------------------------------------------
# mcp
# ---------------------------------------------------------------------------

@cli.command()
@click.argument('site_dir', default='.', metavar='[DIRECTORY]')
def mcp(site_dir):
    """Run an MCP server exposing groups/events/overlays as tools for SITE_DIR."""
    site_dir = _prepare_site_dir(site_dir)
    from calgen.mcp_server import mcp as mcp_app
    mcp_app.run()


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------

@cli.command()
@click.option('--site-dir', default='.', type=click.Path(), help='Site directory (default: .)')
def refresh(site_dir):
    """Fetch iCal feeds and update the event cache."""
    site_dir = _prepare_site_dir(site_dir)
    from calgen.calendars import refresh_calendars
    refresh_calendars()


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

@cli.command()
@click.option('--site-dir', default='.', type=click.Path(), help='Site directory (default: .)')
def pipeline(site_dir):
    """Generate _data/all_events.json from the cached iCal data."""
    site_dir = _prepare_site_dir(site_dir)
    from calgen.pipeline import main as pipeline_main
    pipeline_main()


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

@cli.command()
@click.option('--site-dir', default='.', type=click.Path(), help='Site directory (default: .)')
@click.option('--output-dir', default=None, type=click.Path(),
              help='Build output directory (default: <site-dir>/build)')
def build(site_dir, output_dir):
    """Freeze the site to static HTML files."""
    site_dir = _prepare_site_dir(site_dir)
    from calgen.freeze import main as freeze_main
    freeze_main(site_dir=site_dir, output_dir=output_dir)


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------

@cli.command()
@click.option('--site-dir', default='.', type=click.Path(), help='Site directory (default: .)')
@click.option('--output-dir', default=None, type=click.Path(),
              help='Build output directory (default: <site-dir>/build)')
def rebuild(site_dir, output_dir):
    """Refresh calendars, run the pipeline, then build the static site."""
    site_dir = _prepare_site_dir(site_dir)

    click.echo("[1/3] Refreshing calendars...")
    from calgen.calendars import refresh_calendars
    refresh_calendars()

    click.echo("[2/3] Running data pipeline...")
    from calgen.pipeline import main as pipeline_main
    pipeline_main()

    click.echo("[3/3] Building static site...")
    from calgen.freeze import main as freeze_main
    freeze_main(site_dir=site_dir, output_dir=output_dir)

    click.echo("Done.")


if __name__ == '__main__':
    cli()
