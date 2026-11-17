"""Image analysis and derivatives: palette, difference hash, AVIF thumbnails and previews."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageStat

THUMB_EDGE, THUMB_QUALITY = 720, 50
VIEW_EDGE, VIEW_QUALITY = 2560, 60

# Difference-hash distances (of 64 bits), measured on the real collection:
# resized/recompressed copies <= 3, brightened <= 9, 4% crop <= 12; different images
# start at 5 (near-black ones) and the 1st percentile is 18. A 10% crop is NOT caught.
SAME_DISTANCE = 10
SIMILAR_DISTANCE = 16


def dhash(im: Image.Image) -> str | None:
    """64-bit difference hash as 16 hex chars, or None when the image has too little
    structure for a hash to mean anything (flat or near-black wallpapers all collide)."""
    g = im.convert("L")
    if ImageStat.Stat(g).stddev[0] < 8:
        return None
    px = g.resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    bits = 0
    for y in range(8):
        row = y * 9
        for x in range(8):
            bits = (bits << 1) | (px[row + x] > px[row + x + 1])
    return f"{bits:016x}"


def distance(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    return (int(a, 16) ^ int(b, 16)).bit_count()


def palette_of(im: Image.Image, n: int = 5, min_share: float = 0.03, merge_dist: float = 36.0) -> list[str]:
    """Dominant colours, biggest first; near-identical shades are merged into the biggest."""
    small = im.convert("RGB")
    small.thumbnail((128, 128), Image.Resampling.BOX)
    q = small.quantize(colors=10, method=Image.Quantize.MEDIANCUT, kmeans=2)
    pal = q.getpalette() or []
    counts = sorted(q.getcolors() or [], reverse=True)
    total = sum(c for c, _ in counts) or 1
    groups: list[list] = []  # [r, g, b, pixels]
    for count, idx in counts:
        rgb = pal[idx * 3: idx * 3 + 3]
        for g in groups:
            if sum((a - b) ** 2 for a, b in zip(g[:3], rgb)) ** 0.5 < merge_dist:
                g[3] += count
                break
        else:
            groups.append([*rgb, count])
    out: list[str] = []
    for r, g_, b, count in sorted(groups, key=lambda g: -g[3]):
        if count / total < min_share and out:
            continue
        out.append(f"#{r:02x}{g_:02x}{b:02x}")
        if len(out) == n:
            break
    return out


def analyse(im: Image.Image, size: tuple[int, int] | None = None) -> dict:
    """Everything the catalog stores about an image, from the decoded pixels.
    `size` is the true size when `im` is a reduced decode (JPEG draft mode)."""
    w, h = size or im.size
    work = im.convert("RGB")
    work.thumbnail((768, 768), Image.Resampling.LANCZOS)
    return {
        "width": w,
        "height": h,
        "device": "desktop" if w >= h else "phone",
        "palette": palette_of(work),
        "dhash": dhash(work),
    }


def write_derivatives(im: Image.Image, image_id: str, thumbs_dir: Path, view_dir: Path) -> None:
    rgb = im.convert("RGB")
    for edge, quality, folder in ((THUMB_EDGE, THUMB_QUALITY, thumbs_dir), (VIEW_EDGE, VIEW_QUALITY, view_dir)):
        c = rgb.copy()
        c.thumbnail((edge, edge), Image.Resampling.LANCZOS)  # never upscales
        c.save(folder / f"{image_id}.avif", "AVIF", quality=quality, speed=6)


def open_image(data: bytes) -> Image.Image:
    """Decode and verify untrusted bytes. Raises on anything that is not a real image."""
    probe = Image.open(io.BytesIO(data))
    probe.verify()
    im = Image.open(io.BytesIO(data))
    im.load()
    return im
