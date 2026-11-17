"""Move the archived YAML collection into the database.

    python -m afterimage.migrate --repo /path/to/afterimage --data /path/to/data

Reads <repo>/catalog/works.yaml and wallpapers.yaml, copies each original from the repo
into <data>/originals, copies (or builds) the AVIF thumbnail and preview, and fills the
tables. Safe to run again: images already present are skipped.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import yaml
from PIL import Image

from . import imaging
from .config import Settings
from .db import Database, now


def migrate(repo: Path, settings: Settings, *, log=print) -> dict[str, int]:
    settings.ensure_dirs()
    db = Database(settings.db_path)
    works = yaml.safe_load((repo / "catalog" / "works.yaml").read_text(encoding="utf-8")) or []
    entries = yaml.safe_load((repo / "catalog" / "wallpapers.yaml").read_text(encoding="utf-8")) or []
    stamp = now()
    counts = {"works": 0, "images": 0, "skipped": 0, "derivatives_built": 0, "similar": 0}

    with db.tx() as c:
        for w in works:
            if not db.one("SELECT 1 FROM works WHERE id=?", (w["id"],)):
                c.execute("INSERT INTO works(id, title, kind, links, created_at) VALUES (?,?,?,?,?)",
                          (w["id"], w["title"], w["kind"], json.dumps(w.get("ids") or {}), stamp))
                counts["works"] += 1

    ids = {e["id"] for e in entries}
    for e in entries:
        if db.one("SELECT 1 FROM images WHERE id=?", (e["id"],)):
            counts["skipped"] += 1
            continue
        src = repo / e["path"]
        ext = src.suffix.lower()
        original = f"{e['id']}{ext}"
        shutil.copy2(src, settings.originals_dir / original)

        with Image.open(src) as im:
            true_size = im.size
            if im.format == "JPEG":
                im.draft("RGB", (768, 768))
            info = imaging.analyse(im, size=true_size)
            thumb, view = settings.thumbs_dir / f"{e['id']}.avif", settings.view_dir / f"{e['id']}.avif"
            have = (repo / "site" / "public" / "thumbs" / f"{e['id']}.avif", repo / "site" / "public" / "view" / f"{e['id']}.avif")
            if all(p.is_file() for p in have):
                shutil.copy2(have[0], thumb)
                shutil.copy2(have[1], view)
            else:
                full = Image.open(src)
                imaging.write_derivatives(full, e["id"], settings.thumbs_dir, settings.view_dir)
                counts["derivatives_built"] += 1

        with db.tx() as c:
            c.execute(
                "INSERT INTO images(id, original, caption, device, width, height, bytes, palette, dhash, emotion, reviewed, personal,"
                " confidence, note, source_url, origin, added_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'collection',?,?)",
                (e["id"], original, e["caption"], e["device"], true_size[0], true_size[1], e["bytes"],
                 json.dumps(e.get("palette") or info["palette"]), info["dhash"], (e.get("emotion") or {}).get("primary"),
                 int(bool(e.get("reviewed"))), int(bool(e.get("personal"))), e.get("confidence"), e.get("note"),
                 e.get("source"), stamp, stamp))
            c.executemany("INSERT INTO image_works(image_id, work_id, position) VALUES (?,?,?)",
                          [(e["id"], w, i) for i, w in enumerate(e.get("works", []))])
        counts["images"] += 1
        if counts["images"] % 50 == 0:
            log(f"  {counts['images']} images...")

    with db.tx() as c:
        for e in entries:
            for rank, other in enumerate(e.get("similar") or []):
                if other in ids:
                    c.execute("INSERT OR IGNORE INTO similar(image_id, rank, other_id) VALUES (?,?,?)", (e["id"], rank, other))
                    counts["similar"] += 1
    db.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--data", required=True, type=Path)
    args = ap.parse_args()
    settings = Settings(data_dir=args.data.resolve(), frontend_dir=None, public_url="", admin_password_hash="",
                        session_secret="", api_token="", infovore_mcp="", tmdb_token="")
    print(migrate(args.repo.resolve(), settings))


if __name__ == "__main__":
    main()
