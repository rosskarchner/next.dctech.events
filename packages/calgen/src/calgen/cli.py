#!/usr/bin/env python3
"""
calgen — static site generator for dctech.events.

Commands:
  calgen serve      Run the Flask development server
  calgen pipeline   Generate _data/all_events.json from the exported data
  calgen og-images  Generate a per-event social share card for every event
  calgen og-preview Render one card from CLI args, or a sample grid — no
                    _data/, no DynamoDB export, for fast local iteration on
                    the card design itself
  calgen build      Freeze the site to static HTML

Event data comes from DynamoDB via cdk_next's export_dynamo_to_calgen.py,
which writes the _groups/, _categories/, _single_events/, _recurring_events/,
_overlay/, and _cache/ical/ trees that the pipeline reads. There is no
`refresh` command — the iCal Aggregator Lambda owns feed fetching.
"""
import os
import sys
import click


@click.group()
def cli():
    """calgen: static site generator for dctech.events."""
    pass


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


@cli.command()
@click.option('--site-dir', default='.', type=click.Path(), help='Site directory (default: .)')
def pipeline(site_dir):
    """Generate _data/all_events.json from the exported event data."""
    _prepare_site_dir(site_dir)
    from calgen.pipeline import main as pipeline_main
    pipeline_main()


_OG_PREVIEW_SAMPLES = [
    ('DC Python', 'Monday, September 15, 2026', 'Programming'),
    ('AI/ML Meetup: LLMs & Vector DBs', 'Wednesday, September 10, 2026', 'AI'),
    ('Networking Happy Hour', 'Friday, September 5, 2026', None),
    ('Precision Raster Data for Scanning Tunneling Microscopes',
     'Thursday, August 27, 2026', 'Hardware'),
    ('The Annual Washington DC Metropolitan Area Comprehensive Deep Dive '
     'Workshop on Advanced Distributed Systems Architecture, Kubernetes '
     'Orchestration Patterns, and Cloud-Native Best Practices for '
     'Enterprise Teams', 'Saturday, October 3, 2026', 'Cloud'),
]


@cli.command('og-preview')
@click.option('--title', default=None, help='Event title (omit for a sample grid instead)')
@click.option('--date', 'date_display', default='Monday, January 1, 2026',
              show_default=True, help='Pre-formatted date string')
@click.option('--category', default=None, help='Category display name (omit for none)')
@click.option('--site-name', default='DC Tech Events', show_default=True)
@click.option('--out', default='og-preview.png', show_default=True, type=click.Path(),
              help='Where to write the PNG')
def og_preview(title, date_display, category, site_name, out):
    """Render one social share card, or a sample grid, with no site data.

    No --site-dir, no _data/all_events.json, no DynamoDB export — the point
    is iterating on the card's own design (colors, layout, font sizes in
    og_image.py) as fast as possible. Pass --title for one specific card;
    omit it for a contact sheet covering short/medium/long/no-category
    titles in one image, so a layout change's effect across the real range
    of event titles is visible in a single glance rather than five separate
    files.
    """
    from calgen.og_image import render_event_card, CARD_WIDTH, CARD_HEIGHT
    from PIL import Image

    if title is not None:
        render_event_card(title, date_display, category, site_name).save(out)
        click.echo(f"Wrote {out}")
        return

    cards = [
        render_event_card(t, d, c, site_name) for t, d, c in _OG_PREVIEW_SAMPLES
    ]
    cols = 2
    rows = -(-len(cards) // cols)  # ceil
    sheet = Image.new('RGB', (CARD_WIDTH * cols, CARD_HEIGHT * rows), color=(255, 255, 255))
    for i, card in enumerate(cards):
        x, y = (i % cols) * CARD_WIDTH, (i // cols) * CARD_HEIGHT
        sheet.paste(card, (x, y))
    sheet.save(out)
    click.echo(f"Wrote {out} ({len(cards)} sample cards)")


@cli.command('og-images')
@click.option('--site-dir', default='.', type=click.Path(), help='Site directory (default: .)')
def og_images(site_dir):
    """Generate static/og/{slug}.png for every event in _data/all_events.json.

    Run after `calgen pipeline` (which produces that file) and before
    `calgen build` (Frozen-Flask copies static/ into the frozen output
    wholesale, so these need to already exist there by then).
    """
    site_dir = _prepare_site_dir(site_dir)
    import json
    import os

    from calgen.event_utils import event_slug
    from calgen.routes.common import get_categories
    from calgen.routes.events import _format_event_date
    from calgen.og_image import render_event_card
    from calgen.site_config import get_config

    events_file = os.path.join('_data', 'all_events.json')
    if not os.path.exists(events_file):
        click.echo(f"Error: {events_file} not found — run `calgen pipeline` first", err=True)
        sys.exit(1)
    with open(events_file, 'r') as f:
        events = json.load(f)

    categories = get_categories()
    site_name = get_config().get('site_name', 'Tech Events')
    out_dir = os.path.join(site_dir, 'static', 'og')
    os.makedirs(out_dir, exist_ok=True)

    for event in events:
        slug = event_slug(event)
        category_slugs = event.get('categories') or []
        category_name = categories[category_slugs[0]]['name'] if category_slugs and category_slugs[0] in categories else None
        card = render_event_card(
            event.get('title', 'Untitled Event'),
            _format_event_date(event),
            category_name,
            site_name,
        )
        card.save(os.path.join(out_dir, f'{slug}.png'))

    click.echo(f"Wrote {len(events)} social share cards to {out_dir}")


@cli.command()
@click.option('--site-dir', default='.', type=click.Path(), help='Site directory (default: .)')
@click.option('--output-dir', default=None, type=click.Path(),
              help='Build output directory (default: <site-dir>/build)')
def build(site_dir, output_dir):
    """Freeze the site to static HTML files."""
    site_dir = _prepare_site_dir(site_dir)
    from calgen.freeze import main as freeze_main
    freeze_main(site_dir=site_dir, output_dir=output_dir)


if __name__ == '__main__':
    cli()
