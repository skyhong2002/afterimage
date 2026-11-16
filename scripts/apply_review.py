"""Apply a review file exported from the gallery (open it with ?review=1) to the catalog.

The file holds full snapshots of works / emotion / personal for every image the
reviewer touched, plus any works they created. Applying it marks those entries
`reviewed: true` and drops their AI `confidence` / `note`.

A copy of the file is kept in catalog/reviews/ as provenance.

Usage: uv run scripts/apply_review.py path/to/afterimage-review-*.json [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

import catalog_io

ROOT = catalog_io.ROOT
WORKS = ROOT / "catalog" / "works.yaml"
REVIEWS = ROOT / "catalog" / "reviews"
EMOTIONS = {"joy", "sadness", "anger", "fear", "disgust", "anxiety", "envy", "ennui", "embarrassment"}
KINDS = {"game", "anime", "movie", "show", "book", "music"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = Path(args.file)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        print(f"unsupported review file version: {data.get('version')!r}", file=sys.stderr)
        return 1

    entries = catalog_io.load()
    by_id = {e["id"]: e for e in entries}
    works_text = WORKS.read_text(encoding="utf-8")
    works = yaml.safe_load(works_text)
    known = {w["id"] for w in works}

    problems: list[str] = []
    added = []
    for w in data.get("newWorks", []):
        if not ID_RE.match(str(w.get("id", ""))):
            problems.append(f"new work has a bad id: {w.get('id')!r}")
        elif not str(w.get("title", "")).strip():
            problems.append(f"new work {w['id']} has no title")
        elif w.get("kind") not in KINDS:
            problems.append(f"new work {w['id']} has a bad kind: {w.get('kind')!r}")
        elif w["id"] not in known:
            added.append({"id": w["id"], "title": w["title"].strip(), "kind": w["kind"]})
    known |= {w["id"] for w in added}

    reviews = data.get("reviews", {})
    for wid, r in reviews.items():
        if wid not in by_id:
            problems.append(f"{wid}: not in the catalog")
            continue
        if r.get("emotion") not in EMOTIONS:
            problems.append(f"{wid}: bad emotion {r.get('emotion')!r}")
        for w in r.get("works", []):
            if w not in known:
                problems.append(f"{wid}: unknown work {w!r}")
        if not isinstance(r.get("personal"), bool):
            problems.append(f"{wid}: personal must be true/false")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1

    changed_works = changed_emotion = confirmed = 0
    for wid, r in reviews.items():
        e = by_id[wid]
        old_works, old_emotion = e.get("works", []), e.get("emotion", {}).get("primary")
        if old_works != r["works"]:
            changed_works += 1
        if old_emotion != r["emotion"]:
            changed_emotion += 1
        if old_works == r["works"] and old_emotion == r["emotion"]:
            confirmed += 1
        if r["works"]:
            e["works"] = r["works"]
        else:
            e.pop("works", None)
        e["emotion"] = {"primary": r["emotion"]}
        if r["personal"]:
            e["personal"] = True
        else:
            e.pop("personal", None)
        e.pop("confidence", None)
        e.pop("note", None)
        e["reviewed"] = True

    print(f"{len(reviews)} reviewed: {confirmed} confirmed as suggested, "
          f"{changed_works} works changed, {changed_emotion} emotions changed; "
          f"{len(added)} new works {[w['id'] for w in added]}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    if added:
        works = sorted(works + added, key=lambda w: (w["kind"], w["title"].lower()))
        header = "".join(l + "\n" for l in works_text.splitlines() if l.startswith("#"))
        WORKS.write_text(header + yaml.safe_dump(works, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8")
    catalog_io.save(entries)
    REVIEWS.mkdir(exist_ok=True)
    dest = REVIEWS / f"{datetime.now():%Y-%m-%d-%H%M}-{path.name}"
    shutil.copy(path, dest)
    print(f"written; review file kept at {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
