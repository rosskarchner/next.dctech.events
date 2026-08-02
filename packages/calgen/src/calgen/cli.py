#!/usr/bin/env python3
"""
calgen — static site generator for dctech.events.

Commands:
  calgen serve     Run the Flask development server
  calgen pipeline  Generate _data/all_events.json from the exported data
  calgen build     Freeze the site to static HTML

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
