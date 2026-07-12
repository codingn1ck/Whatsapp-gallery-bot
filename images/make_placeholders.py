#!/usr/bin/env python3
"""
Generate placeholder artwork images.

These are NOT art. They are obviously-fake labelled cards, sized to each
artwork's real aspect ratio, so that:

  - the WhatsApp image pipeline can be tested end to end
  - nobody can mistake them for the gallery's actual works
  - the demo shows a real image arriving on a real phone

Swap them for the gallery's real photographs when the catalog arrives.
Filenames are read from the catalog CSV, so they always match.
"""

from __future__ import annotations

import csv
import hashlib
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CATALOG = "data/catalog.csv"
OUT_DIR = Path("images")
LONG_EDGE = 1000  # WhatsApp downscales anyway; this is plenty

# Muted, gallery-ish palette. Deterministic per artwork so each is distinct.
PALETTE = [
    (94, 106, 120), (120, 98, 92), (86, 104, 92), (110, 96, 116),
    (126, 112, 84), (88, 96, 116), (114, 88, 88), (96, 112, 104),
]


def font(size: int):
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def wrap(draw, text, fnt, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def make(row: dict) -> None:
    # Real aspect ratio from the catalog's dimensions ("120 x 90")
    try:
        w_cm, h_cm = (float(x.strip()) for x in row["dimensions_cm"].split("x"))
    except Exception:
        w_cm, h_cm = 4.0, 3.0

    if w_cm >= h_cm:
        W = LONG_EDGE
        H = max(300, int(LONG_EDGE * h_cm / w_cm))
    else:
        H = LONG_EDGE
        W = max(300, int(LONG_EDGE * w_cm / h_cm))

    seed = int(hashlib.md5(row["id"].encode()).hexdigest()[:8], 16)
    base = PALETTE[seed % len(PALETTE)]

    img = Image.new("RGB", (W, H), base)
    d = ImageDraw.Draw(img)

    # Subtle vertical wash so it doesn't look like a flat error page
    for y in range(H):
        k = 1.0 - (y / H) * 0.22
        d.line([(0, y), (W, y)], fill=tuple(int(c * k) for c in base))

    # Inner frame
    m = int(min(W, H) * 0.05)
    d.rectangle([m, m, W - m, H - m], outline=(235, 232, 226), width=3)

    pad = m + int(min(W, H) * 0.06)
    box = W - 2 * pad

    f_title = font(max(26, int(min(W, H) * 0.062)))
    f_meta = font(max(17, int(min(W, H) * 0.034)))
    f_note = font(max(14, int(min(W, H) * 0.026)))

    lines = wrap(d, row["title"], f_title, box)
    meta = f"{row['artist']} · {row['year']}"
    sub = f"{row['medium']} · {row['dimensions_cm']} cm"

    th = len(lines) * (f_title.size + 8) + f_meta.size + f_note.size + 34
    y = (H - th) // 2

    for ln in lines:
        d.text(((W - d.textlength(ln, font=f_title)) / 2, y), ln,
               font=f_title, fill=(245, 243, 239))
        y += f_title.size + 8

    y += 10
    d.text(((W - d.textlength(meta, font=f_meta)) / 2, y), meta,
           font=f_meta, fill=(224, 219, 210))
    y += f_meta.size + 8
    d.text(((W - d.textlength(sub, font=f_note)) / 2, y), sub,
           font=f_note, fill=(198, 192, 182))

    # Unmistakably a placeholder.
    tag = "PLACEHOLDER — NOT ACTUAL ARTWORK"
    f_tag = font(max(12, int(min(W, H) * 0.021)))
    d.text(((W - d.textlength(tag, font=f_tag)) / 2, H - pad + 4), tag,
           font=f_tag, fill=(168, 162, 152))

    out = OUT_DIR / row["image_file"]
    img.save(out, "JPEG", quality=88)
    print(f"  {out}  ({W}x{H})")


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    with open(CATALOG, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    print(f"generating {len(rows)} placeholders -> {OUT_DIR}/")
    for r in rows:
        make(r)
    print("\ndone. Push images/ to GitHub, then set:")
    print("  IMAGE_BASE_URL=https://raw.githubusercontent.com/"
          "<user>/<repo>/main/images")


if __name__ == "__main__":
    main()
