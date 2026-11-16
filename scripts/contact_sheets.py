"""Render numbered contact sheets of the catalog, for reviewing many images at once.

Each tile shows the thumbnail and its index in catalog/wallpapers.yaml.
Output goes outside the repo (default /tmp/afterimage-sheets).

Usage: uv run scripts/contact_sheets.py [--out DIR] [--per-sheet 20] [--only a,b,c]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import catalog_io

ROOT = catalog_io.ROOT
THUMBS = ROOT / "site" / "public" / "thumbs"
COLS, TILE_W, TILE_H, GAP = 5, 360, 215, 6
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def font(size: int) -> ImageFont.ImageFont:
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/afterimage-sheets")
    ap.add_argument("--per-sheet", type=int, default=20)
    ap.add_argument("--only", help="comma-separated catalog indexes to render instead of everything")
    args = ap.parse_args()

    entries = catalog_io.load()
    indexes = [int(i) for i in args.only.split(",")] if args.only else list(range(len(entries)))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("sheet-*.jpg"):
        old.unlink()

    label_font = font(22)
    rows = -(-args.per_sheet // COLS)
    written = 0
    for s in range(0, len(indexes), args.per_sheet):
        chunk = indexes[s : s + args.per_sheet]
        sheet = Image.new("RGB", (COLS * (TILE_W + GAP) + GAP, rows * (TILE_H + GAP) + GAP), (18, 18, 22))
        draw = ImageDraw.Draw(sheet)
        for k, idx in enumerate(chunk):
            x = GAP + (k % COLS) * (TILE_W + GAP)
            y = GAP + (k // COLS) * (TILE_H + GAP)
            with Image.open(THUMBS / f"{entries[idx]['id']}.avif") as im:
                im = im.convert("RGB")
                im.thumbnail((TILE_W, TILE_H), Image.Resampling.LANCZOS)
                sheet.paste(im, (x + (TILE_W - im.width) // 2, y + (TILE_H - im.height) // 2))
            tag = str(idx)
            w = draw.textlength(tag, font=label_font)
            draw.rectangle((x, y, x + w + 12, y + 28), fill=(0, 0, 0))
            draw.text((x + 6, y + 2), tag, fill=(255, 235, 59), font=label_font)
        name = out / f"sheet-{s // args.per_sheet:02d}.jpg"
        sheet.save(name, quality=85)
        written += 1
    print(f"{written} sheets for {len(indexes)} images -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
