"""Render the weekly newsletter from DynamoDB via calgen's own pipeline.

Deliberate modernization over production (which HTTP-scrapes the live site's
prerendered newsletter.html/.txt): materialize a calgen site dir in /tmp from
DynamoDB using the same export used by the Static Site Generator, run
calgen's pipeline (which applies remove_duplicates — the "also published by"
merge logic), and render calgen's own /newsletter.html and /newsletter.txt
routes through a Flask test client. Zero reimplementation of render logic.
"""
import os
import shutil
import subprocess  # nosec
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_SRC = os.path.join(HERE, 'site')
SITE_DIR = '/tmp/calgen-site'  # nosec - Lambda scratch space


def render_newsletter():
    """Return (html, text) of the current newsletter."""
    # Fresh scratch site from the bundled source (config/templates/static/regions)
    if os.path.isdir(SITE_DIR):
        shutil.rmtree(SITE_DIR)
    shutil.copytree(SITE_SRC, SITE_DIR)

    cwd = os.getcwd()
    os.chdir(SITE_DIR)
    os.environ['CALGEN_SITE_DIR'] = SITE_DIR
    try:
        # Export DynamoDB → calgen layout (same script CodeBuild uses)
        subprocess.run(  # nosec
            [sys.executable, os.path.join(HERE, 'export_dynamo_to_calgen.py')],
            check=True, cwd=SITE_DIR)

        from calgen.site_config import reset_config
        reset_config()
        from calgen.pipeline import main as pipeline_main
        pipeline_main()

        from calgen.app import create_app
        app = create_app(SITE_DIR)
        client = app.test_client()

        html_resp = client.get('/newsletter.html')
        text_resp = client.get('/newsletter.txt')
        if html_resp.status_code != 200 or text_resp.status_code != 200:
            raise RuntimeError(
                f'newsletter render failed: html={html_resp.status_code} '
                f'text={text_resp.status_code}')
        return (html_resp.get_data(as_text=True),
                text_resp.get_data(as_text=True))
    finally:
        os.chdir(cwd)
