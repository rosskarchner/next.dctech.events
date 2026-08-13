# Site images

These files are **generated**, not hand-drawn. Do not edit them directly —
regenerate from the site's own branding instead:

```bash
python scripts/generate_site_images.py
```

The script reads the wordmark face from `site/static/kenney-mini/kenney-mini.woff2`
and mirrors the palette in `site/static/css/main.css`, so a rename, a new
tagline or a palette change only needs the script re-run. It needs `pillow`,
`fonttools`, and a brotli decoder (`pip install brotlicffi`) to read the
`.woff2`.

## What it produces

| File | Size | Used by |
| --- | --- | --- |
| `og-image.png` | 1200×630 | `og:image` / `twitter:image` in `base.html` — the social share card |
| `favicon-16.png`, `favicon-32.png` | 16, 32 | browser tabs; the 32 also shows in Google mobile results |
| `favicon.ico` | 16/32/48 | clients that still probe `/favicon.ico` directly |
| `icon-180.png` | 180 | iOS home screen (`apple-touch-icon`) |
| `icon-192.png`, `icon-512.png` | 192, 512 | `site/static/manifest.json` |

An earlier version of this file described responsive header images
(`header-mobile.png` and friends) and pointed at a `HEADER_IMAGES.md` that was
never written. The site has no header image and the templates never referenced
those files, so they are not generated. `SIZES_QUICK_REFERENCE.txt` is left
from that plan and describes sizes nothing uses.
