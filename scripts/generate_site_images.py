#!/usr/bin/env python3
"""Generate the site's social card and favicons from the site's own branding.

These are build inputs, not hand-drawn art: the colors come from the CSS
variables in site/static/css/main.css and the wordmark uses Kenney Mini, the
same face the site header uses. Regenerate whenever the name, tagline or
palette changes:

    python scripts/generate_site_images.py

Requires: pillow, fonttools, and a brotli decoder (brotlicffi or brotli) so
fontTools can read the .woff2 the site ships.
"""
import argparse
import io
import os
import sys

from PIL import Image, ImageDraw, ImageFont

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(REPO, 'site')
WOFF2 = os.path.join(SITE, 'static', 'kenney-mini', 'kenney-mini.woff2')
OUT_DIR = os.path.join(SITE, 'static', 'images')

# Mirrors :root in site/static/css/main.css.
COLOR_TEXT = (31, 41, 55)
COLOR_TEXT_LIGHT = (107, 114, 128)
COLOR_PRIMARY = (37, 99, 235)
COLOR_BG = (255, 255, 255)

SITE_NAME = 'DC Tech Events'
TAGLINE = 'Technology conferences and meetups'
TAGLINE_2 = 'in and around Washington, DC'
MARK = 'DC'


def load_kenney_ttf():
    """Kenney Mini ships as .woff2; PIL needs sfnt, so convert in memory."""
    from fontTools.ttLib import TTFont
    font = TTFont(WOFF2)
    font.flavor = None
    buf = io.BytesIO()
    font.save(buf)
    buf.seek(0)
    return buf.read()


def sized(ttf_bytes, size):
    return ImageFont.truetype(io.BytesIO(ttf_bytes), size)


def _fallback(size):
    for path in ('/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def centered(draw, text, font, cx, y, fill):
    left, top, right, _ = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (right - left) / 2 - left, y - top), text, font=font, fill=fill)


def build_og_image(ttf, path):
    """1200x630 — the size Facebook, LinkedIn, Slack and X all read."""
    img = Image.new('RGB', (1200, 630), COLOR_BG)
    d = ImageDraw.Draw(img)

    # Accent bar along the top, echoing the site's link color.
    d.rectangle([0, 0, 1200, 18], fill=COLOR_PRIMARY)

    centered(d, SITE_NAME, sized(ttf, 108), 600, 210, COLOR_TEXT)
    centered(d, TAGLINE, _fallback(40), 600, 380, COLOR_TEXT_LIGHT)
    centered(d, TAGLINE_2, _fallback(40), 600, 436, COLOR_TEXT_LIGHT)

    d.rectangle([0, 612, 1200, 630], fill=COLOR_PRIMARY)
    img.save(path, 'PNG', optimize=True)
    return img.size


def build_icon(ttf, size, path):
    """Square app/tab icon: the wordmark's initials, knocked out of blue.

    Rendered at 8x and downsampled so the pixel font's edges survive the
    small sizes instead of turning to mush.
    """
    scale = 8
    big = Image.new('RGB', (size * scale, size * scale), COLOR_PRIMARY)
    d = ImageDraw.Draw(big)
    font = sized(ttf, int(size * scale * 0.44))
    left, top, right, bottom = d.textbbox((0, 0), MARK, font=font)
    d.text(((size * scale - (right - left)) / 2 - left,
            (size * scale - (bottom - top)) / 2 - top),
           MARK, font=font, fill=(255, 255, 255))
    big.resize((size, size), Image.LANCZOS).save(path, 'PNG', optimize=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-dir', default=OUT_DIR)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    try:
        ttf = load_kenney_ttf()
    except ImportError as exc:
        print(f"error: {exc}\nInstall a brotli decoder: pip install brotlicffi",
              file=sys.stderr)
        return 1

    written = []
    p = os.path.join(args.out_dir, 'og-image.png')
    build_og_image(ttf, p)
    written.append(p)

    for size in (16, 32, 180, 192, 512):
        name = f'favicon-{size}.png' if size in (16, 32) else f'icon-{size}.png'
        p = os.path.join(args.out_dir, name)
        build_icon(ttf, size, p)
        written.append(p)

    # Multi-resolution .ico for browsers and crawlers that still probe /favicon.ico.
    ico = os.path.join(args.out_dir, 'favicon.ico')
    Image.open(os.path.join(args.out_dir, 'icon-192.png')).save(
        ico, sizes=[(16, 16), (32, 32), (48, 48)])
    written.append(ico)

    for p in written:
        print(f"  {os.path.relpath(p, REPO)}  {os.path.getsize(p):,} bytes")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
