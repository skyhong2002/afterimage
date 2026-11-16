"""Merge suggested labels from a TSV into catalog/wallpapers.yaml.

TSV columns (pipe separated, one row per image, index = position in the catalog):
  index | work ids (comma separated, may be empty) | emotion | confidence (h/m/l) | note

Writes, per entry: works, emotion {primary}, confidence, reviewed: false, and
`personal: true` / `note` where relevant. Entries with `reviewed: true` are never
touched. Work ids must exist in catalog/works.yaml.

Usage: uv run scripts/apply_labels.py catalog/labels/<file>.tsv
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

import catalog_io

EMOTIONS = {"joy", "sadness", "anger", "fear", "disgust", "anxiety", "envy", "ennui", "embarrassment"}
WORKS = catalog_io.ROOT / "catalog" / "works.yaml"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    rows = [l.rstrip("\n").split("|", 4) for l in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if l.strip()]
    entries = catalog_io.load()
    known = {w["id"] for w in yaml.safe_load(WORKS.read_text(encoding="utf-8"))}

    problems = []
    applied = skipped = 0
    for idx, works, emotion, conf, note in rows:
        e = entries[int(idx)]
        ids = [w for w in works.split(",") if w]
        if emotion not in EMOTIONS:
            problems.append(f"{idx}: unknown emotion {emotion!r}")
        for w in ids:
            if w not in known:
                problems.append(f"{idx}: work {w!r} is not in works.yaml")
        if conf not in {"h", "m", "l"}:
            problems.append(f"{idx}: bad confidence {conf!r}")
        if e.get("reviewed") is True:
            skipped += 1
            continue
        for k in ("works", "emotion", "personal", "confidence", "reviewed", "note"):
            e.pop(k, None)
        if ids:
            e["works"] = ids
        e["emotion"] = {"primary": emotion}
        if "personal" in note:
            e["personal"] = True
        e["confidence"] = conf
        e["reviewed"] = False
        if conf != "h" and note:
            e["note"] = note
        applied += 1

    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    catalog_io.save(entries)
    print(f"applied {applied}, skipped {skipped} already reviewed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
