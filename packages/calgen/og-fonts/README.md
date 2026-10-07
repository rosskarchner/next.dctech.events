# Social card fonts

Used by `src/calgen/og_image.py` to draw OG/share cards. **Not part of the calgen
wheel** (it lives outside `src/`): the wheel is also installed into Lambdas that
never draw cards. `cdk_next/build_lambdas.sh` copies this directory into the
CodeBuild source zip, and the buildspec points `CALGEN_OG_FONT_DIR` at it. From a
checkout, og_image.py finds it on its own.

| File | What | Source | License |
|---|---|---|---|
| `NotoSans-Regular.ttf` | Text (Latin, Greek, Cyrillic, punctuation, symbols) | notofonts/notofonts.github.io, `fonts/NotoSans/hinted/ttf/` | SIL OFL 1.1, `NotoSans-OFL.txt` |
| `NotoColorEmoji.ttf` | Color emoji (CBDT bitmaps, one 109 ppem strike) | Debian `fonts-noto-color-emoji` 2.051-1, built from googlefonts/noto-emoji | SIL OFL 1.1, `NotoColorEmoji-LICENSE.txt` |

Why this emoji font and not another: Pillow draws CBDT bitmap emoji. It cannot
draw COLRv1 (the format google/fonts and Fedora ship) and draws nothing for COLRv0
(Twemoji Mozilla), both tried. noto-emoji no longer publishes a prebuilt CBDT file.

To update: replace the two `.ttf` files and keep the file names. Glyphs a font lacks
are dropped from cards rather than drawn as boxes (`og_image._prepare`), so a stale
font degrades quietly; `python -m calgen.og_image` writes a preview.
