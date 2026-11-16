"""Scan wallpaper folders and (re)build catalog/wallpapers.yaml.

Derived fields (path, caption, device, size, bytes) are refreshed on every run.
Everything else on an existing entry (works, emotion, core, reviewed, ...) is
preserved, so hand edits and later enrichment are never overwritten.

Usage: uv run scripts/build_catalog.py
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "wallpapers.yaml"
FOLDERS = {"wallpaper": "desktop", "phone wallpaper": "phone"}
EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def content_id(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def main() -> int:
    existing: dict[str, dict] = {}
    if CATALOG.exists():
        for entry in yaml.safe_load(CATALOG.read_text(encoding="utf-8")) or []:
            existing[entry["id"]] = entry

    entries: dict[str, dict] = {}
    duplicates: list[tuple[str, str]] = []

    for folder, device in FOLDERS.items():
        for path in sorted((ROOT / folder).iterdir(), key=lambda p: p.name.lower()):
            if path.suffix.lower() not in EXTS:
                continue
            wid = content_id(path)
            rel = path.relative_to(ROOT).as_posix()
            if wid in entries:
                duplicates.append((entries[wid]["path"], rel))
                continue
            with Image.open(path) as im:
                width, height = im.size
            derived = {
                "id": wid,
                "path": rel,
                "caption": path.stem,
                "device": device,
                "size": [width, height],
                "bytes": path.stat().st_size,
            }
            entries[wid] = {**existing.get(wid, {}), **derived}

    ordered = sorted(entries.values(), key=lambda e: e["path"].lower())
    CATALOG.parent.mkdir(exist_ok=True)
    CATALOG.write_text(
        yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )

    removed = set(existing) - set(entries)
    print(f"{len(ordered)} wallpapers -> {CATALOG.relative_to(ROOT)}")
    print(f"  desktop: {sum(e['device'] == 'desktop' for e in ordered)}, "
          f"phone: {sum(e['device'] == 'phone' for e in ordered)}")
    if removed:
        print(f"  dropped {len(removed)} entries whose file is gone: {sorted(removed)}")
    for kept, dup in duplicates:
        print(f"  DUPLICATE content: {dup!r} == {kept!r} (skipped)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
