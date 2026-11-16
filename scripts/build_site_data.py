"""Generate AVIF thumbnails/previews and site/src/data/catalog.json from the YAML catalog.

- thumbs/<id>.avif : grid thumbnail (long edge 720)
- view/<id>.avif   : lightbox preview (long edge 2560, never upscaled)

Originals are NOT shipped with the site; the site links to them on GitHub raw.
Outputs are cached by id (content hash), so only new images are processed.

Usage: uv run scripts/build_site_data.py
"""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "wallpapers.yaml"
PUBLIC = ROOT / "site" / "public"
OUT = ROOT / "site" / "src" / "data" / "catalog.json"
WORKS = ROOT / "catalog" / "works.yaml"
WORKS_OUT = ROOT / "site" / "src" / "data" / "works.json"
API_OUT = ROOT / "site" / "public" / "api" / "wallpapers.json"

# (subdir, long edge, quality)
VARIANTS = [("thumbs", 720, 50), ("view", 2560, 60)]


def render(job: tuple[str, str]) -> int:
    src, wid = job
    made = 0
    im: Image.Image | None = None
    for sub, edge, quality in VARIANTS:
        dst = PUBLIC / sub / f"{wid}.avif"
        if dst.exists():
            continue
        if im is None:
            im = Image.open(ROOT / src).convert("RGB")
        c = im.copy()
        c.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        c.save(dst, "AVIF", quality=quality, speed=6)
        made += 1
    return made


def main() -> int:
    entries = yaml.safe_load(CATALOG.read_text(encoding="utf-8")) or []
    for sub, *_ in VARIANTS:
        (PUBLIC / sub).mkdir(parents=True, exist_ok=True)

    with ProcessPoolExecutor() as pool:
        made = sum(pool.map(render, [(e["path"], e["id"]) for e in entries], chunksize=4))

    # drop outputs for images that no longer exist in the catalog
    keep = {e["id"] for e in entries}
    for sub, *_ in VARIANTS:
        for f in (PUBLIC / sub).iterdir():
            if f.stem not in keep:
                f.unlink()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")

    works = yaml.safe_load(WORKS.read_text(encoding="utf-8")) if WORKS.exists() else []
    WORKS_OUT.write_text(json.dumps(works, ensure_ascii=False, indent=1), encoding="utf-8")

    # Public machine-readable catalog for other services (infovore, Hermes).
    # Personal photos are left out on purpose. Deterministic: no timestamps.
    public = [e for e in entries if not e.get("personal")]
    used = {w for e in public for w in e.get("works", [])}
    api = {
        "version": 1,
        "works": [
            {**w, "count": sum(w["id"] in e.get("works", []) for e in public)}
            for w in works
            if w["id"] in used
        ],
        "wallpapers": [
            {
                "id": e["id"],
                "caption": e["caption"],
                "device": e["device"],
                "width": e["size"][0],
                "height": e["size"][1],
                "works": e.get("works", []),
                "emotion": e.get("emotion", {}).get("primary"),
                "palette": e.get("palette", []),
                "reviewed": bool(e.get("reviewed")),
            }
            for e in public
        ],
    }
    API_OUT.parent.mkdir(parents=True, exist_ok=True)
    API_OUT.write_text(json.dumps(api, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(
        f"{len(entries)} entries, {made} new files -> {OUT.relative_to(ROOT)}; {len(works)} works; "
        f"public api: {len(api['wallpapers'])} wallpapers ({len(entries) - len(public)} personal left out)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
